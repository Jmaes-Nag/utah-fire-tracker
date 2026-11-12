"""Feed caching, freshness headers and upstream validation."""

import asyncio
import time
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Response
from shapely.geometry import Point, shape

from app import arcgis, cache, config, feeds, geometry, util

from .support import BackendTest, arc_feature, boundary_feature, collection, ring


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
