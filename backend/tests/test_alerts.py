"""Alert engine: threat geometry, CAP warnings and derived freshness."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
from shapely.geometry import box

from app import alerts, arcgis, boundaries, feeds, util
from app import weather as weather_feed
from app.feeds import calculate_pm25_aqi
from app.geometry import threat_geometry

from .support import BackendTest, ring


class AlertTests(BackendTest):
    def alert_sources(self, incidents=None, warnings=None, weather=None):
        self.replace(feeds, "get_incidents", AsyncMock(return_value=incidents or []))
        self.replace(arcgis, "fetch_arcgis", AsyncMock(return_value={"features": warnings or []}))
        self.replace(
            weather_feed,
            "get_weather",
            AsyncMock(
                return_value=weather
                or {"wind_speed_mph": 0, "wind_direction_deg": None, "wind_direction_cardinal": "N/A"}
            ),
        )

    def fire(self, lon=-111.0, lat=40.0):
        return {"id": "fire-1", "name": "Test Fire", "latitude": lat, "longitude": lon}

    async def test_fresh_alerts_keep_array_contract_and_timestamp(self):
        self.alert_sources(incidents=[self.fire()])
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()[0]["risk_level"], 2)
        self.assertEqual(result.json()[0]["fires"][0]["id"], "fire-1")
        self.assertTrue(result.headers["X-Data-Timestamp"])
        self.assertNotIn("X-Data-Stale", result.headers)

    async def test_failed_incidents_preserve_previous_alerts_not_cached_all_clear(self):
        self.alert_sources()
        feeds.get_incidents.side_effect = HTTPException(status_code=503)
        alerts.alerts_cache.update(data=[{"city": "Previous Risk"}], expiry=0, created_at="old")
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), [{"city": "Previous Risk"}])
        self.assertEqual(result.headers["X-Data-Stale"], "true")
        self.assertEqual(result.headers["X-Data-Timestamp"], "old")
        self.assertEqual(alerts.alerts_cache["expiry"], 0)

    async def test_failed_incidents_without_cache_return_503(self):
        self.alert_sources()
        feeds.get_incidents.side_effect = HTTPException(status_code=502)
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 503)
        self.assertIsNone(alerts.alerts_cache["data"])

    async def test_missing_boundaries_never_returns_false_empty_alerts(self):
        boundaries.muni_boundaries.clear()
        self.replace(arcgis, "fetch_arcgis", AsyncMock(side_effect=util.UpstreamError()))
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 503)

    async def test_cap_failure_preserves_previous_warning(self):
        self.alert_sources()
        arcgis.fetch_arcgis.side_effect = util.UpstreamError()
        alerts.alerts_cache.update(data=[{"city": "CAP Risk"}], created_at="old", expiry=0)
        result = await self.api("/api/alerts")
        self.assertEqual(result.json(), [{"city": "CAP Risk"}])
        self.assertEqual(result.headers["X-Data-Stale"], "true")

    async def test_cold_weather_failure_returns_visible_base_risk_marked_stale(self):
        self.alert_sources(incidents=[self.fire()])
        weather_feed.get_weather.side_effect = HTTPException(status_code=502)
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()[0]["risk_level"], 2)
        self.assertEqual(result.headers["X-Data-Stale"], "true")
        self.assertTrue(result.headers["X-Data-Timestamp"])
        self.assertIsNone(alerts.alerts_cache["data"])
        self.assertEqual(alerts.alerts_cache["expiry"], 0)

    async def test_stale_incident_dependency_marks_alerts_stale(self):
        self.alert_sources()

        async def stale_incidents(response):
            response.headers["X-Data-Stale"] = "true"
            return [self.fire()]

        feeds.get_incidents.side_effect = stale_incidents
        result = await self.api("/api/alerts")
        self.assertEqual(result.headers["X-Data-Stale"], "true")
        self.assertIsNone(alerts.alerts_cache["data"])

    async def test_stale_weather_dependency_does_not_clear_prior_plume_warning(self):
        self.alert_sources(incidents=[self.fire()])

        async def stale_weather(**kwargs):
            kwargs["response"].headers["X-Data-Stale"] = "true"
            return {"wind_speed_mph": 0, "wind_direction_deg": None}

        weather_feed.get_weather.side_effect = stale_weather
        alerts.alerts_cache.update(data=[{"city": "Downwind City"}], created_at="old", expiry=0)
        result = await self.api("/api/alerts")
        self.assertEqual({item["city"] for item in result.json()}, {"Downwind City", "Test City"})
        self.assertEqual(result.headers["X-Data-Stale"], "true")

    async def test_wind_corridor_includes_middle_between_distant_endpoints(self):
        boundaries.muni_boundaries[0]["geometry"] = box(-111.005, 40.135, -110.995, 40.145)
        self.alert_sources(
            incidents=[self.fire()],
            weather={"wind_speed_mph": 100, "wind_direction_deg": 180, "wind_direction_cardinal": "S"},
        )
        result = await alerts.get_alerts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["risk_level"], 1)

    async def test_east_west_five_mile_radius_accounts_for_latitude(self):
        boundaries.muni_boundaries[0]["geometry"] = box(-110.917, 40, -110.916, 40.001)
        self.alert_sources(incidents=[self.fire()])
        result = await alerts.get_alerts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["risk_level"], 2)
        self.assertLess(result[0]["fires"][0]["distance_miles"], 5)

    async def test_cap_second_exterior_ring_hits_utah_city(self):
        self.alert_sources(
            warnings=[
                {
                    "attributes": {"event": "Wildfire", "status": "Actual"},
                    "geometry": {"rings": [ring(-120, 40), ring()]},
                }
            ]
        )
        result = await alerts.get_alerts()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["risk_level"], 2)
        self.assertEqual(len(result[0]["fema_alerts"]), 1)

    def test_expired_test_cancelled_future_cap_records_are_ignored(self):
        attributes = [
            {"expires": "2000-01-01T00:00:00Z"},
            {"expires": 1000000000000},
            {"status": "Test"},
            {"status": "Exercise"},
            {"msgType": "Cancel"},
            {"effective": "2999-01-01T00:00:00Z"},
        ]
        records = [{"attributes": attrs, "geometry": {"rings": [ring()]}} for attrs in attributes]
        self.assertEqual(alerts.parse_cap({"features": records}), [])

    async def test_malformed_live_cap_keeps_previous_alerts(self):
        self.alert_sources(warnings=[{"attributes": {"event": "Wildfire"}, "geometry": None}])
        alerts.alerts_cache.update(data=[{"city": "Previous CAP"}], expiry=0, created_at="old")
        result = await self.api("/api/alerts")
        self.assertEqual(result.json(), [{"city": "Previous CAP"}])
        self.assertEqual(result.headers["X-Data-Stale"], "true")

    async def test_concurrent_alert_requests_share_computation(self):
        self.alert_sources(incidents=[self.fire()])
        results = await asyncio.gather(*(alerts.get_alerts() for _ in range(15)))
        self.assertTrue(all(results[0] == result for result in results))
        self.assertEqual(feeds.get_incidents.await_count, 1)
        self.assertEqual(arcgis.fetch_arcgis.await_count, 1)
        self.assertEqual(weather_feed.get_weather.await_count, 1)

    async def test_weather_requests_are_bounded(self):
        self.alert_sources(incidents=[{**self.fire(), "id": str(i)} for i in range(20)])
        active, peak = 0, 0

        async def weather(**kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return {"wind_speed_mph": 0, "wind_direction_deg": None}

        weather_feed.get_weather.side_effect = weather
        await alerts.get_alerts()
        self.assertLessEqual(peak, 8)

    async def test_alert_freshness_cannot_outlive_oldest_dependency(self):
        self.alert_sources(incidents=[self.fire()])
        now = time.time()
        feeds.incidents_cache.update(data=[self.fire()], expiry=now + 100, created_at="2026-11-11T12:00:00Z")
        weather_feed.weather_cache["40.000,-111.000"] = {
            "data": {},
            "expiry": now + 20,
            "created_at": "2026-11-11T11:00:00Z",
        }
        result = await self.api("/api/alerts")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(alerts.alerts_cache["expiry"], now + 20)
        self.assertEqual(result.headers["X-Data-Timestamp"], "2026-11-11T11:00:00Z")

    async def test_mixed_valid_invalid_cap_snapshot_is_degraded(self):
        valid = {"attributes": {"event": "Wildfire"}, "geometry": {"rings": [ring()]}}
        self.alert_sources(incidents=[self.fire()], warnings=[valid, {"geometry": None}])
        response = await self.api("/api/alerts")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Data-Stale"], "true")
        self.assertIsNone(alerts.alerts_cache["data"])


class TestAlertsEngine(unittest.TestCase):
    def test_south_wind_projects_north_and_retains_fire_buffer(self):
        base, plume, _, _ = threat_geometry(40.6608, -111.8910, 20.0, 180.0)
        city = box(-111.90, 40.75, -111.88, 40.77)
        self.assertFalse(base.intersects(city))
        self.assertTrue(plume.intersects(city))
        self.assertTrue(plume.covers(base))

    def test_calm_weather_uses_base_buffer(self):
        base, plume, _, _ = threat_geometry(40.6608, -111.8910, 0.0, None)
        self.assertTrue(base.equals(plume))

    def test_aqi_breakpoints(self):
        # EPA concentration truncation precedes interpolation and AQI rounding.
        expected = {
            0.0: 0,
            4.5: 25,
            9.0: 50,
            9.1: 51,
            22.25: 75,
            35.4: 100,
            35.5: 101,
            45.45: 125,
            55.4: 150,
            225.5: 301,
            325.4: 500,
            500.0: 500,
            600.0: 500,
        }
        for concentration, aqi in expected.items():
            with self.subTest(concentration=concentration):
                self.assertEqual(calculate_pm25_aqi(concentration), aqi)
