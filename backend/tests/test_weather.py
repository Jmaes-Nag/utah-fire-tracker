"""Weather parsing, station fallback and the per-point cache."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import HTTPException

from app import config
from app import weather as weather_feed

from .support import BackendTest, observation


class WeatherTests(BackendTest):
    def weather_transport(self, observations):
        self.calls = []

        def handler(request):
            self.calls.append(request)
            if "/points/" in request.url.path:
                payload = {"properties": {"observationStations": "https://api.weather.gov/gridpoints/SLC/1,2/stations"}}
            elif request.url.path.endswith("/stations"):
                payload = {
                    "features": [
                        {"properties": {"stationIdentifier": station, "name": station}} for station in observations
                    ]
                }
            else:
                payload = observations[request.url.path.split("/")[2]]
                if isinstance(payload, Exception):
                    raise payload
            return httpx.Response(200, json=payload)

        self.transport(handler)

    async def test_numeric_strings_and_optional_malformed_values(self):
        payload = observation()
        payload["properties"]["relativeHumidity"] = {"value": "nan"}
        payload["properties"]["temperature"] = "invalid"
        self.weather_transport({"KSLC": payload})
        result = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["wind_speed_mph"], 22.4)
        self.assertEqual(result.json()["wind_direction_deg"], 180.0)
        self.assertEqual(result.json()["wind_direction_cardinal"], "S")
        self.assertIsNone(result.json()["temperature_c"])
        self.assertIsNone(result.json()["relative_humidity"])
        self.assertIn("X-Data-Timestamp", result.headers)

    async def test_wind_speed_units_and_calm_missing_direction(self):
        for unit, speed, expected in [
            ("wmoUnit:km_h-1", 36, 22.4),
            ("wmoUnit:m_s-1", 10, 22.4),
            ("wmoUnit:mi_h-1", 20, 20.0),
            ("wmoUnit:kn", 10, 11.5),
        ]:
            with self.subTest(unit=unit):
                self.assertEqual(
                    weather_feed.parse_weather(observation(speed, 180, unit)["properties"], "KSLC", "SLC")[
                        "wind_speed_mph"
                    ],
                    expected,
                )
        data = weather_feed.parse_weather(observation(0, None)["properties"], "KSLC", "SLC")
        self.assertEqual(data["wind_speed_mph"], 0)
        self.assertIsNone(data["wind_direction_deg"])
        self.assertEqual(data["wind_direction_cardinal"], "N/A")

    async def test_station_fallback_after_bad_units_nonfinite_or_network_failure(self):
        for first in [
            observation(unit="mystery"),
            observation(speed="nan"),
            observation(direction="bad"),
            observation(speed=-1),
            observation(direction=361),
            httpx.ReadTimeout("timeout"),
        ]:
            with self.subTest(first=str(first)[:40]):
                weather_feed.weather_cache.clear()
                self.weather_transport({"BAD": first, "GOOD": observation(10, 270, "wmoUnit:m_s-1")})
                result = await self.api("/api/weather?latitude=40&longitude=-111")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["station_id"], "GOOD")

    async def test_concurrent_same_rounded_weather_point_is_coalesced(self):
        self.weather_transport({"KSLC": observation()})
        results = await asyncio.gather(*(weather_feed.get_weather(40.00001 + i * 0.000001, -111) for i in range(10)))
        self.assertEqual(len(self.calls), 3)
        self.assertIn("/points/40.000,-111.000", str(self.calls[0].url))
        self.assertEqual(len(weather_feed.weather_cache), 1)
        self.assertEqual(results[0]["temperature_c"], 20.0)

    async def test_weather_outage_preserves_stale_value_and_observation_timestamp(self):
        weather_feed.weather_cache["40.000,-111.000"] = {
            "data": {"station_id": "OLD", "timestamp": "original"},
            "expiry": 0,
            "created_at": "original",
        }
        self.transport(lambda request: httpx.Response(200, text="invalid json"))
        response = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Data-Stale"], "true")
        self.assertEqual(response.headers["X-Data-Timestamp"], "original")
        self.assertEqual(response.json()["timestamp"], "original")

    async def test_old_missing_future_and_naive_observations_try_next_station(self):
        timestamps = [
            (datetime.now(UTC) - timedelta(hours=3)).isoformat(),
            None,
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "2026-11-11T12:00:00",
        ]
        for timestamp in timestamps:
            with self.subTest(timestamp=timestamp):
                weather_feed.weather_cache.clear()
                old = observation()
                old["properties"]["timestamp"] = timestamp
                self.weather_transport({"OLD": old, "FRESH": observation()})
                result = await self.api("/api/weather?latitude=40&longitude=-111")
                self.assertEqual(result.json()["station_id"], "FRESH")

    async def test_weather_freshness_uses_observation_time_and_caps_expiry(self):
        payload = observation()
        observed = datetime.now(UTC) - timedelta(seconds=config.MAX_OBSERVATION_AGE_SECS - 30)
        payload["properties"]["timestamp"] = observed.isoformat()
        self.weather_transport({"KSLC": payload})
        result = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(result.headers["X-Data-Timestamp"], observed.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertAlmostEqual(
            weather_feed.weather_cache["40.000,-111.000"]["expiry"],
            observed.timestamp() + config.MAX_OBSERVATION_AGE_SECS,
            places=3,
        )

    async def test_disallowed_station_link_is_not_fetched(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"properties": {"observationStations": "http://169.254.169.254/latest"}})

        self.transport(handler)
        self.assertEqual((await self.api("/api/weather?latitude=40&longitude=-111")).status_code, 502)
        self.assertEqual(len(calls), 1)

    def test_url_allowlist_rejects_credentials_ports_nonstring_and_malformed(self):
        self.assertTrue(weather_feed.is_allowed_nws_url("https://api.weather.gov/stations"))
        for url in [
            None,
            123,
            "http://api.weather.gov/",
            "https://api.weather.gov.evil/",
            "https://user@api.weather.gov/",
            "https://api.weather.gov:444/",
            "https://api.weather.gov:bad/",
            "https://[bad",
        ]:
            with self.subTest(url=url):
                self.assertFalse(weather_feed.is_allowed_nws_url(url))

    async def test_oversubscribed_weather_cache_is_bounded(self):
        self.replace(config, "WEATHER_CACHE_LIMIT", 2)
        self.weather_transport({"KSLC": observation()})
        for latitude in (40, 41, 42):
            await weather_feed.get_weather(latitude, -111)
        self.assertEqual(len(weather_feed.weather_cache), 2)
        self.assertNotIn("40.000,-111.000", weather_feed.weather_cache)

    async def test_nws_lookups_for_distinct_points_share_a_global_concurrency_cap(self):
        self.replace(weather_feed, "nws_fetch_slots", asyncio.Semaphore(2))
        release = asyncio.Event()
        in_flight = peak = 0

        async def handler(request):
            nonlocal in_flight, peak
            if "/points/" in request.url.path:
                in_flight += 1
                peak = max(peak, in_flight)
                await release.wait()
                in_flight -= 1
                stations = "https://api.weather.gov/gridpoints/SLC/1,2/stations"
                return httpx.Response(200, json={"properties": {"observationStations": stations}})
            if request.url.path.endswith("/stations"):
                return httpx.Response(200, json={"features": [{"properties": {"stationIdentifier": "KSLC"}}]})
            return httpx.Response(200, json=observation())

        self.transport(handler)
        lookups = [asyncio.create_task(weather_feed.get_weather(40 + i * 0.01, -111)) for i in range(6)]
        await asyncio.sleep(0.05)
        self.assertEqual(peak, 2)
        release.set()
        results = await asyncio.gather(*lookups)
        self.assertEqual(peak, 2)
        self.assertTrue(all(result["station_id"] == "KSLC" for result in results))
        self.assertEqual(len(weather_feed.weather_cache), 6)

    async def test_saturated_nws_slots_fail_fast_without_upstream_calls(self):
        self.replace(config, "NWS_SLOT_WAIT_SECS", 0.01)
        self.replace(weather_feed, "nws_fetch_slots", asyncio.Semaphore(1))
        await weather_feed.nws_fetch_slots.acquire()
        self.weather_transport({"KSLC": observation()})
        response = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.calls, [])
        # The failure is backed off like any upstream error, so retries of this point stay off NWS too.
        self.assertGreater(weather_feed.weather_cache["40.000,-111.000"]["retry_after"], 0)

    async def test_saturated_nws_slots_still_serve_cached_and_stale_points(self):
        self.replace(config, "NWS_SLOT_WAIT_SECS", 0.01)
        self.replace(weather_feed, "nws_fetch_slots", asyncio.Semaphore(1))
        self.weather_transport({"KSLC": observation()})
        self.assertEqual((await self.api("/api/weather?latitude=40&longitude=-111")).status_code, 200)
        await weather_feed.nws_fetch_slots.acquire()
        fresh = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(fresh.status_code, 200)
        self.assertNotIn("X-Data-Stale", fresh.headers)
        weather_feed.weather_cache["40.000,-111.000"]["expiry"] = 0
        stale = await self.api("/api/weather?latitude=40&longitude=-111")
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(stale.headers["X-Data-Stale"], "true")
        self.assertEqual(len(self.calls), 3)

    async def test_cancelled_lookup_releases_its_nws_slot(self):
        self.replace(weather_feed, "nws_fetch_slots", asyncio.Semaphore(1))
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        self.transport(handler)
        lookup = asyncio.create_task(weather_feed.get_weather(40, -111))
        await started.wait()
        self.assertTrue(weather_feed.nws_fetch_slots.locked())
        lookup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await lookup
        self.assertFalse(weather_feed.nws_fetch_slots.locked())

    async def test_coordinates_outside_existing_utah_buffer_rejected(self):
        for lat, lon in [(0, 0), (float("nan"), -111), (40, float("inf"))]:
            with self.subTest(lat=lat, lon=lon), self.assertRaises(HTTPException) as caught:
                await weather_feed.get_weather(lat, lon)
            self.assertEqual(caught.exception.status_code, 400)
