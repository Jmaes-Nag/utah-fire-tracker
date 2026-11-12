"""Municipal boundary loading, persistence and recovery."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import alerts, arcgis, boundaries, config, feeds, main, util
from app import weather as weather_feed

from .support import BackendTest, boundary_feature, collection


class StartupTests(BackendTest):
    async def test_bundled_path_is_module_relative_and_loads_without_network(self):
        self.assertEqual(config.BOUNDARIES_PATH.parent, Path(config.__file__).resolve().parent / "data")
        fetch = self.replace(
            arcgis, "fetch_arcgis", AsyncMock(side_effect=AssertionError("Network should not be used"))
        )
        # A fresh checkout has no ignored SGID download. Supply a local fixture
        # so this test verifies disk loading without relying on developer data.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            path.write_text(json.dumps(collection([boundary_feature()])))
            self.replace(config, "BOUNDARIES_PATH", path)
            await boundaries.load_boundaries()
            self.assertEqual(len(boundaries.muni_boundaries), 1)
        fetch.assert_not_called()

    async def test_corrupt_file_is_recovered_and_atomically_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            path.write_text("truncated json")
            self.replace(config, "BOUNDARIES_PATH", path)
            self.replace(arcgis, "fetch_arcgis", AsyncMock(return_value=collection([boundary_feature()])))
            await boundaries.load_boundaries()
            self.assertEqual(boundaries.muni_boundaries[0]["name"], "Test City")
            self.assertEqual(json.loads(path.read_text())["type"], "FeatureCollection")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    async def test_readonly_persistence_does_not_discard_valid_download(self):
        with tempfile.TemporaryDirectory() as directory:
            self.replace(config, "BOUNDARIES_PATH", Path(directory) / "missing.geojson")
            self.replace(arcgis, "fetch_arcgis", AsyncMock(return_value=collection([boundary_feature()])))
            self.replace(boundaries, "persist_boundaries", unittest.mock.Mock(side_effect=PermissionError("readonly")))
            self.replace(boundaries, "muni_boundaries", [])
            await boundaries.load_boundaries()
            self.assertEqual(len(boundaries.muni_boundaries), 1)

    async def test_failed_replacement_preserves_old_disk_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            path.write_text("old data")
            self.replace(config, "BOUNDARIES_PATH", path)
            with patch.object(os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                boundaries.persist_boundaries(collection([boundary_feature()]))
            self.assertEqual(path.read_text(), "old data")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    async def test_invalid_download_never_overwrites_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            path.write_text("corrupt but recoverable later")
            self.replace(config, "BOUNDARIES_PATH", path)
            self.replace(arcgis, "fetch_arcgis", AsyncMock(return_value={"error": "unavailable"}))
            self.replace(boundaries, "muni_boundaries", [])
            await boundaries.load_boundaries()
            self.assertEqual(path.read_text(), "corrupt but recoverable later")
            self.assertEqual(boundaries.muni_boundaries, [])

    async def test_mixed_boundary_snapshot_is_rejected_instead_of_silently_omitting_cities(self):
        data = collection([{"geometry": None}, boundary_feature(), {"geometry": {"type": "Point"}}])
        with self.assertRaises(ValueError):
            boundaries.parse_boundaries(data)

    async def test_bad_boundary_recovery_keeps_previous_complete_memory_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            path.write_text(json.dumps(collection([boundary_feature(), {"geometry": None}])))
            self.replace(config, "BOUNDARIES_PATH", path)
            self.replace(arcgis, "fetch_arcgis", AsyncMock(side_effect=util.UpstreamError()))
            previous = boundaries.muni_boundaries
            await boundaries.load_boundaries()
            self.assertIs(boundaries.muni_boundaries, previous)

    async def test_lifespan_loads_boundaries(self):
        startup = self.replace(boundaries, "load_boundaries", AsyncMock())
        async with main.app.router.lifespan_context(main.app):
            startup.assert_awaited_once()

    async def test_failed_startup_download_backs_off_then_recovers_on_alert_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundaries.geojson"
            self.replace(config, "BOUNDARIES_PATH", path)
            self.replace(boundaries, "muni_boundaries", [])
            self.replace(
                feeds,
                "get_incidents",
                AsyncMock(return_value=[{"id": "fire-1", "name": "Test Fire", "latitude": 40.0, "longitude": -111.0}]),
            )
            self.replace(
                weather_feed,
                "get_weather",
                AsyncMock(
                    return_value={"wind_speed_mph": 0, "wind_direction_deg": None, "wind_direction_cardinal": "N/A"}
                ),
            )
            fetch = self.replace(
                arcgis,
                "fetch_arcgis",
                AsyncMock(
                    side_effect=[
                        util.UpstreamError("startup sgid outage"),
                        collection([boundary_feature()]),
                        {"features": []},
                    ]
                ),
            )
            await boundaries.load_boundaries()
            self.assertEqual(boundaries.muni_boundaries, [])
            self.assertEqual(boundaries.boundary_state["failures"], 1)
            self.assertGreater(boundaries.boundary_state["retry_after"], time.time())

            # During boundary backoff window, alert refresh does not re-query SGID.
            during_backoff = await self.api("/api/alerts")
            self.assertEqual(during_backoff.status_code, 503)
            self.assertEqual(fetch.await_count, 1)

            # Once backoff expires, alert refresh recovers boundaries, persists them, and computes alerts.
            boundaries.boundary_state["retry_after"] = 0.0
            alerts.alerts_cache["retry_after"] = 0.0
            recovered = await self.api("/api/alerts")
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json()[0]["city"], "Test City")
            self.assertEqual(len(boundaries.muni_boundaries), 1)
            self.assertEqual(boundaries.boundary_state["failures"], 0)
            self.assertEqual(boundaries.boundary_state["retry_after"], 0.0)
            self.assertTrue(path.exists())

    async def test_boundary_recovery_backoff_is_exponential_bounded_and_single_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            self.replace(config, "BOUNDARIES_PATH", Path(directory) / "boundaries.geojson")
            self.replace(boundaries, "muni_boundaries", [])
            clock = [1000.0]
            calls = 0

            async def failing_fetch(*args, **kwargs):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0)
                raise util.UpstreamError("sgid down")

            self.replace(arcgis, "fetch_arcgis", failing_fetch)
            with patch.object(time, "time", side_effect=lambda: clock[0]):
                results = await asyncio.gather(*(boundaries.recover_boundaries() for _ in range(10)))
                self.assertEqual(results, [False] * 10)
                self.assertEqual(calls, 1)
                self.assertEqual(boundaries.boundary_state["retry_after"], 1030.0)

                expected_delays = [60, 120, 240, 480, 900, 900]
                for delay in expected_delays:
                    clock[0] = boundaries.boundary_state["retry_after"]
                    self.assertFalse(await boundaries.recover_boundaries())
                    self.assertEqual(boundaries.boundary_state["retry_after"], clock[0] + delay)

    async def test_background_boundary_recovery_loop_is_bounded_and_cancels_in_lifespan(self):
        with tempfile.TemporaryDirectory() as directory:
            self.replace(config, "BOUNDARIES_PATH", Path(directory) / "boundaries.geojson")
            self.replace(boundaries, "muni_boundaries", [])
            self.replace(arcgis, "fetch_arcgis", AsyncMock(side_effect=util.UpstreamError("sgid down")))
            clock = [1000.0]
            sleeps = []

            async def fake_sleep(delay):
                sleeps.append(delay)
                clock[0] += delay

            with (
                patch.object(time, "time", side_effect=lambda: clock[0]),
                patch.object(asyncio, "sleep", side_effect=fake_sleep),
            ):
                self.assertFalse(await boundaries.boundary_recovery_loop(max_attempts=3))
                self.assertEqual(arcgis.fetch_arcgis.await_count, 3)
                self.assertEqual(sleeps, [30.0, 60.0])

            # Verify lifespan starts and cleanly stops background recovery when boundaries are missing.
            self.replace(boundaries, "load_boundaries", AsyncMock())
            started = asyncio.Event()

            async def blocked_loop():
                started.set()
                await asyncio.Event().wait()

            self.replace(boundaries, "boundary_recovery_loop", blocked_loop)
            async with main.app.router.lifespan_context(main.app):
                await started.wait()
