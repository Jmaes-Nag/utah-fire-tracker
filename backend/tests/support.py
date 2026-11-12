"""Shared fixtures and the base test case for offline backend tests."""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from app import alerts, boundaries, cache, feeds, main
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
