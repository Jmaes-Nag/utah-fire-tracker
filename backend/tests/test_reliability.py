"""Offline regression tests for production failure paths and API contracts."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException, Response
from shapely.geometry import Point, box, shape

from app import alerts, arcgis, boundaries, cache, config, feeds, geometry, main, util
from app import weather as weather_feed

HTTP_CLIENT = httpx.AsyncClient


def arc_feature(lon=-111.0, lat=40.0, **attrs):
    return {"attributes": attrs, "geometry": {"x": lon, "y": lat}}


def ring(lon=-111.0, lat=40.0, size=0.01):
    return [[lon, lat], [lon + size, lat], [lon + size, lat + size], [lon, lat + size], [lon, lat]]


def boundary_feature():
    return {
        "type": "Feature",
        "properties": {"NAME": "Test City", "OBJECTID": 1, "FIPS": "123"},
        "geometry": {"type": "Polygon", "coordinates": [ring()]},
    }


def collection(features):
    return {"type": "FeatureCollection", "features": features}


def observation(speed="36", direction="180", unit="wmoUnit:km_h-1"):
    return {
        "properties": {
            "windSpeed": {"value": speed, "unitCode": unit},
            "windDirection": {"value": direction},
            "temperature": {"value": "20", "unitCode": "wmoUnit:degC"},
            "relativeHumidity": {"value": "25"},
            "timestamp": datetime.now(UTC).isoformat(),
        }
    }


class BackendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = []
        for name in ("incidents", "perimeters", "hotspots", "smoke", "aqi"):
            self.replace(feeds, name + "_cache", cache.new_cache())
        self.replace(alerts, "alerts_cache", cache.new_cache())
        self.replace(weather_feed, "weather_cache", {})
        self.replace(boundaries, "boundary_state", boundaries.new_boundary_state())
        self.replace(boundaries, "muni_boundaries", boundaries.parse_boundaries(collection([boundary_feature()])))

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def replace(self, module, name, value):
        patcher = patch.object(module, name, value)
        self.patches.append(patcher)
        patcher.start()
        return value

    def transport(self, handler):
        def factory(**kwargs):
            return HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        patcher = patch.object(httpx, "AsyncClient", side_effect=factory)
        self.patches.append(patcher)
        patcher.start()

    async def api(self, path):
        async with HTTP_CLIENT(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            return await client.get(path, headers={"Origin": "https://dashboard.example"})


class FeedCacheTests(BackendTest):
    endpoints = ("incidents", "perimeters", "hotspots", "smoke", "aqi")

    def entry(self, endpoint):
        return getattr(feeds, endpoint + "_cache")

    async def test_fresh_incident_response_has_exposed_timestamp_and_no_stale(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"features": [arc_feature(UniqueFireIdentifier="fire-1")]})

        self.transport(handler)
        first = await self.api("/api/incidents")
        second = await self.api("/api/incidents")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()[0]["id"], "fire-1")
        self.assertTrue(first.headers["X-Data-Timestamp"].endswith("Z"))
        self.assertEqual(first.headers["X-Data-Timestamp"], second.headers["X-Data-Timestamp"])
        self.assertNotIn("X-Data-Stale", first.headers)
        self.assertIn("X-Data-Stale", first.headers["Access-Control-Expose-Headers"])
        self.assertEqual(len(calls), 1)
        self.assertIn("US-UT", calls[0].url.params["where"])

    async def test_every_feed_preserves_stale_data_and_timestamp_on_upstream_errors(self):
        self.transport(lambda request: httpx.Response(200, json={"error": {"message": "outage"}}))
        for endpoint in self.endpoints:
            with self.subTest(endpoint=endpoint):
                entry = self.entry(endpoint)
                data = collection([]) if endpoint == "perimeters" else []
                entry.update(data=data, created_at="2026-11-01T00:00:00Z", expiry=0)
                result = await self.api("/api/" + endpoint)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), data)
                self.assertEqual(result.headers["X-Data-Stale"], "true")
                self.assertEqual(result.headers["X-Data-Timestamp"], "2026-11-01T00:00:00Z")
                self.assertEqual(entry["expiry"], 0)

    async def test_cold_bad_feeds_return_502_instead_of_500_or_false_empty(self):
        bad_payloads = [[], {}, {"features": None}, {"features": {}}, {"error": "outage"}, {"features": [None]}]
        for payload in bad_payloads:
            with patch.object(arcgis, "fetch_arcgis", wraps=arcgis.fetch_arcgis):
                self.transport(lambda request, payload=payload: httpx.Response(200, json=payload))
                for endpoint in self.endpoints:
                    with self.subTest(endpoint=endpoint, payload=payload):
                        self.entry(endpoint).clear()
                        self.entry(endpoint).update(cache.new_cache())
                        result = await self.api("/api/" + endpoint)
                        self.assertEqual(result.status_code, 502, result.text)
                        self.assertIsNone(self.entry(endpoint)["data"])

    async def test_cold_transport_failures_are_503_and_coalesced(self):
        calls = []

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0)
            raise httpx.ConnectError("offline", request=request)

        self.transport(handler)
        results = await asyncio.gather(*(self.api("/api/incidents") for _ in range(20)))
        self.assertEqual({result.status_code for result in results}, {503})
        self.assertEqual(len(calls), 1)

    async def test_concurrent_refreshes_are_single_flight(self):
        calls = []

        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"features": [arc_feature()]})

        self.transport(handler)
        results = await asyncio.gather(*(feeds.get_incidents(Response()) for _ in range(20)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(result == results[0] for result in results))

    async def test_empty_feed_is_valid_and_cached(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=collection([]))

        self.transport(handler)
        for endpoint in self.endpoints:
            for _ in range(2):
                result = await self.api("/api/" + endpoint)
                self.assertEqual(result.status_code, 200)
                self.assertNotIn("X-Data-Stale", result.headers)
        self.assertEqual(len(calls), len(self.endpoints))

    async def test_refresh_ttl_begins_after_download_finishes(self):
        clock = [1000.0]

        async def load():
            clock[0] = 1100.0
            return []

        with patch.object(time, "time", side_effect=lambda: clock[0]):
            await cache.cached(feeds.incidents_cache, load)
        self.assertEqual(feeds.incidents_cache["expiry"], 1100 + config.CACHE_DURATION_SECS)

    async def test_retry_after_outage_recovers_and_clears_stale_header(self):
        feeds.incidents_cache.update(data=[{"id": "old"}], expiry=0, created_at="old")
        loader = AsyncMock(side_effect=[util.UpstreamError(), []])
        response = Response()
        await cache.cached(feeds.incidents_cache, loader, response)
        self.assertEqual(response.headers["X-Data-Stale"], "true")
        feeds.incidents_cache["retry_after"] = 0
        self.assertEqual(await cache.cached(feeds.incidents_cache, loader, response), [])
        self.assertNotIn("X-Data-Stale", response.headers)
        self.assertEqual(loader.await_count, 2)

    async def test_cancelled_refresh_does_not_poison_lock_or_cache(self):
        started = asyncio.Event()

        async def blocked():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(cache.cached(feeds.incidents_cache, blocked))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(await cache.cached(feeds.incidents_cache, AsyncMock(return_value=[])), [])

    async def test_smoke_transport_failure_tries_replica(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            if len(calls) == 1:
                raise httpx.ReadTimeout("primary timed out", request=request)
            return httpx.Response(200, json={"features": [{"attributes": {"FID": 0}, "geometry": {"rings": [ring()]}}]})

        self.transport(handler)
        result = await self.api("/api/smoke")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()[0]["fid"], 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("Replica", calls[1])

    async def test_smoke_both_services_fail_does_not_cache_all_clear(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"error": "failed"})

        self.transport(handler)
        result = await self.api("/api/smoke")
        self.assertEqual(result.status_code, 502)
        self.assertIsNone(feeds.smoke_cache["data"])
        self.assertEqual(len(calls), 2)

    async def test_arcgis_pagination_includes_all_records(self):
        offsets = []

        def handler(request):
            offset = request.url.params.get("resultOffset")
            offsets.append(offset)
            return httpx.Response(
                200,
                json={
                    "features": [arc_feature(OBJECTID=1 if offset is None else 2)],
                    "exceededTransferLimit": offset is None,
                },
            )

        self.transport(handler)
        result = await feeds.get_incidents()
        self.assertEqual([item["object_id"] for item in result], [1, 2])
        self.assertEqual(offsets, [None, "1"])

    async def test_arcgis_repeated_page_is_rejected(self):
        self.transport(
            lambda request: httpx.Response(200, json={"features": [arc_feature()], "exceededTransferLimit": True})
        )
        result = await self.api("/api/incidents")
        self.assertEqual(result.status_code, 502)
        self.assertIsNone(feeds.incidents_cache["data"])

    async def test_geojson_pagination_reads_nested_transfer_limit(self):
        offsets = []

        def handler(request):
            offset = request.url.params.get("resultOffset")
            offsets.append(offset)
            feature = boundary_feature()
            feature["id"] = len(offsets)
            return httpx.Response(
                200, json={**collection([feature]), "properties": {"exceededTransferLimit": offset is None}}
            )

        self.transport(handler)
        result = await feeds.get_perimeters()
        self.assertEqual(len(result["features"]), 2)
        self.assertEqual(offsets, [None, "1"])

    async def test_arcgis_non_json_and_http_status_are_gateway_errors(self):
        for result in [httpx.Response(200, text="<html>outage</html>"), httpx.Response(500)]:
            feeds.incidents_cache.clear()
            feeds.incidents_cache.update(cache.new_cache())
            self.transport(lambda request, result=result: result)
            self.assertEqual((await self.api("/api/incidents")).status_code, 502)


class FeedValidationTests(BackendTest):
    async def test_optional_omissions_stay_disclosed_on_cache_hits_and_clear_after_recovery(self):
        cases = [
            ("perimeters", collection([boundary_feature(), {"type": "Feature", "geometry": None}])),
            ("hotspots", {"features": [arc_feature(FID=1), {}]}),
            ("smoke", {"features": [{"attributes": {"FID": 1}, "geometry": {"rings": [ring()]}}, {}]}),
            ("aqi", {"features": [arc_feature(value=10, unit="ug/m3"), {}]}),
        ]
        for endpoint, payload in cases:
            with self.subTest(endpoint=endpoint):
                loader = AsyncMock(return_value=payload)
                self.replace(arcgis, "fetch_arcgis", loader)
                entry = getattr(feeds, endpoint + "_cache")
                for _ in range(2):
                    response = await self.api("/api/" + endpoint)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["X-Data-Stale"], "true")
                    data = response.json()
                    self.assertEqual(len(data["features"] if endpoint == "perimeters" else data), 1)
                self.assertEqual(loader.await_count, 1)
                loader.return_value = {**payload, "features": payload["features"][:1]}
                entry["expiry"] = 0
                recovered = await self.api("/api/" + endpoint)
                self.assertEqual(recovered.status_code, 200)
                self.assertNotIn("X-Data-Stale", recovered.headers)

    async def test_incidents_filter_prescribed_and_out_fires_preserve_complexes_unknown(self):
        features = [
            arc_feature(OBJECTID=i, IncidentTypeCategory=category, FireOutDateTime=out)
            for i, (category, out) in enumerate(
                [("RX", None), ("WF", 1700000000000), ("WF", None), ("CX", None), (None, None), ("WF", 0)]
            )
        ]
        self.transport(lambda request: httpx.Response(200, json={"features": features}))
        result = await feeds.get_incidents()
        self.assertEqual([item["object_id"] for item in result], [2, 3, 4, 5])

    async def test_all_prescribed_burns_is_a_valid_empty_wildfire_snapshot(self):
        self.transport(lambda request: httpx.Response(200, json={"features": [arc_feature(IncidentTypeCategory="RX")]}))
        response = await self.api("/api/incidents")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.assertNotIn("X-Data-Stale", response.headers)

    async def test_perimeters_apply_matching_category_and_out_filters(self):
        features = []
        for category, out in [("RX", None), ("WF", 1700000000000), ("WF", None), ("CX", None)]:
            feature = boundary_feature()
            feature["properties"].update(attr_IncidentTypeCategory=category, attr_FireOutDateTime=out)
            features.append(feature)
        self.transport(lambda request: httpx.Response(200, json=collection(features)))
        result = await feeds.get_perimeters()
        self.assertEqual(
            [feature["properties"]["attr_IncidentTypeCategory"] for feature in result["features"]], ["WF", "CX"]
        )

    async def test_mixed_incident_records_reject_incomplete_refresh(self):
        features = [
            None,
            {},
            {"attributes": None, "geometry": None},
            arc_feature(lon="NaN"),
            arc_feature(lon=999),
            arc_feature(lon=-120),
            arc_feature(POOState="US-CA"),
            arc_feature(lon="-111", lat="40", IncidentSize="123.4", PercentContained="nan", UniqueFireIdentifier="ok"),
        ]
        self.transport(lambda request: httpx.Response(200, json={"features": features}))
        result = await self.api("/api/incidents")
        self.assertEqual(result.status_code, 502)
        self.assertIsNone(feeds.incidents_cache["data"])

    async def test_incidents_normalize_finite_numbers(self):
        self.transport(
            lambda request: httpx.Response(
                200,
                json={
                    "features": [
                        arc_feature(
                            lon="-111",
                            lat="40",
                            IncidentSize="123.4",
                            PercentContained="nan",
                            UniqueFireIdentifier="ok",
                        )
                    ]
                },
            )
        )
        result = await self.api("/api/incidents")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()), 1)
        self.assertEqual(result.json()[0]["acres"], 123.4)
        self.assertIsNone(result.json()[0]["containment"])

    async def test_mixed_incident_refresh_retains_last_complete_snapshot(self):
        feeds.incidents_cache.update(data=[{"id": "previous"}], expiry=0, created_at="original")
        self.transport(lambda request: httpx.Response(200, json={"features": [arc_feature(), {"geometry": None}]}))
        result = await self.api("/api/incidents")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), [{"id": "previous"}])
        self.assertEqual(result.headers["X-Data-Stale"], "true")
        self.assertEqual(result.headers["X-Data-Timestamp"], "original")

    async def test_bad_optional_field_types_are_gateway_errors_not_crashes(self):
        for endpoint, feature in [("incidents", arc_feature(POOState=[])), ("aqi", arc_feature(value=9, unit=[]))]:
            with self.subTest(endpoint=endpoint):
                self.transport(lambda request, feature=feature: httpx.Response(200, json={"features": [feature]}))
                self.assertEqual((await self.api("/api/" + endpoint)).status_code, 502)

    async def test_nonfinite_json_metadata_cannot_break_serialization(self):
        self.transport(
            lambda request: httpx.Response(
                200, text='{"features":[{"attributes":{"OBJECTID":NaN},"geometry":{"x":-111,"y":40}}]}'
            )
        )
        self.assertEqual((await self.api("/api/incidents")).status_code, 502)

    async def test_perimeters_skip_bad_geometry_repair_self_intersection(self):
        valid = boundary_feature()
        invalid = [
            {"geometry": None},
            {"geometry": {"type": "Point", "coordinates": [-111, 40]}},
            {"geometry": {"type": "Polygon", "coordinates": [[[1, 2]]]}},
        ]
        bowtie = deepcopy(valid)
        bowtie["geometry"]["coordinates"] = [[[-111, 40], [-110.9, 40.1], [-111, 40.1], [-110.9, 40], [-111, 40]]]
        self.transport(lambda request: httpx.Response(200, json=collection(invalid + [valid, bowtie])))
        result = await self.api("/api/perimeters")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["features"]), 2)
        for feature in result.json()["features"]:
            self.assertTrue(shape(feature["geometry"]).is_valid)

    async def test_hotspots_skip_nonfinite_and_foreign_points(self):
        self.transport(
            lambda request: httpx.Response(
                200, json={"features": [arc_feature(lon="inf"), arc_feature(lon=-120), arc_feature(FRP="7.5")]}
            )
        )
        result = await self.api("/api/hotspots")
        self.assertEqual(len(result.json()), 1)
        self.assertEqual(result.json()[0]["frp"], 7.5)

    async def test_aqi_missing_negative_invalid_values_are_not_good_air(self):
        features = [arc_feature(value=value) for value in [None, -1, "nan", "bad", True]]
        features += [arc_feature(value="9.09", unit="µg/m³"), arc_feature(value=9, unit="ppm")]
        self.transport(lambda request: httpx.Response(200, json={"features": features}))
        result = await self.api("/api/aqi")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()), 1)
        self.assertEqual(result.json()[0]["aqi"], 50)

    def test_aqi_current_epa_upper_breakpoints_and_truncation(self):
        expected = {
            9.09: 50,
            9.1: 51,
            35.49: 100,
            55.4: 150,
            125.4: 200,
            225.4: 300,
            225.5: 301,
            325.4: 500,
            500: 500,
            1e308: 500,
        }
        for pm25, aqi in expected.items():
            with self.subTest(pm25=pm25):
                self.assertEqual(feeds.calculate_pm25_aqi(pm25), aqi)

    def test_arcgis_multiple_shells_holes_and_nested_islands(self):
        outer = ring(size=0.1)
        hole = ring(-110.98, 40.02, 0.05)
        island = ring(-110.97, 40.03, 0.01)
        separate = ring(-110.5, 40.0, 0.01)
        geom = geometry.arcgis_polygon([hole, separate, outer, island])
        self.assertTrue(geom.covers(Point(-110.495, 40.005)))
        self.assertTrue(geom.covers(Point(-110.995, 40.005)))
        self.assertFalse(geom.covers(Point(-110.975, 40.025)))
        self.assertTrue(geom.covers(Point(-110.965, 40.035)))

    def test_polygon_rejects_nonfinite_open_and_degenerate_rings(self):
        for coords in [
            [[0, 0], [1, 0], [1, 1], [0, 1]],
            [[-111, 40], [-111, 40], [-111, 40], [-111, 40]],
            [[-111, 40], [-110, 40], [-110, float("nan")], [-111, 40]],
        ]:
            with self.subTest(coords=coords), self.assertRaises(ValueError):
                geometry.arcgis_polygon([coords])


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

    async def test_coordinates_outside_existing_utah_buffer_rejected(self):
        for lat, lon in [(0, 0), (float("nan"), -111), (40, float("inf"))]:
            with self.subTest(lat=lat, lon=lon), self.assertRaises(HTTPException) as caught:
                await weather_feed.get_weather(lat, lon)
            self.assertEqual(caught.exception.status_code, 400)


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


if __name__ == "__main__":
    unittest.main()
