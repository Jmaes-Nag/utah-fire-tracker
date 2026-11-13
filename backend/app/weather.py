"""NWS station observations for a point, with a bounded per-point cache."""

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from . import config
from .cache import LoadedData, Snapshot, new_cache, refresh
from .util import UpstreamError, finite_number, mapping, utc_stamp

logger = logging.getLogger("wildfire-tracker")
router = APIRouter()

weather_cache: dict[str, Any] = {}
# Cache hits never take a slot; only lookups that actually call NWS do (see observation_snapshot).
nws_fetch_slots = asyncio.Semaphore(config.NWS_FETCH_CONCURRENCY)


def is_allowed_nws_url(url: str | None) -> bool:
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        return bool(
            parsed
            and parsed.scheme == "https"
            and parsed.hostname in config.ALLOWED_NWS_HOSTS
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def get_cardinal_direction(degrees: float | None) -> str:
    degrees = finite_number(degrees)
    if degrees is None:
        return "N/A"
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degrees % 360 + 11.25) / 22.5) % 16]


def quantity(properties, name):
    obj = mapping(properties.get(name))
    return finite_number(obj.get("value")), obj.get("unitCode")


def observation_time(value):
    if not isinstance(value, str):
        raise ValueError("Missing observation timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Observation timestamp has no timezone")
    timestamp = parsed.timestamp()
    age = time.time() - timestamp
    if not -300 <= age <= config.MAX_OBSERVATION_AGE_SECS:
        raise ValueError("Observation is too old or in the future")
    return timestamp


def parse_weather(properties, station_id, station_name):
    observation_time(properties.get("timestamp"))
    speed, unit = quantity(properties, "windSpeed")
    direction, _ = quantity(properties, "windDirection")
    if speed is None or speed < 0 or (speed > 0 and (direction is None or not 0 <= direction <= 360)):
        raise ValueError("Missing or invalid wind measurements")
    conversions = {
        "wmoUnit:km_h-1": 1.0,
        "wmoUnit:m_s-1": 3.6,
        "wmoUnit:mi_h-1": 1.609344,
        "wmoUnit:kn": 1.852,
        "wmoUnit:kt": 1.852,
    }
    if unit not in conversions:
        raise ValueError("Unknown wind speed unit")
    kmh = speed * conversions[unit]
    if not math.isfinite(kmh):
        raise ValueError("Wind unit conversion overflowed")
    temp, temp_unit = quantity(properties, "temperature")
    if temp is not None:
        if temp_unit == "wmoUnit:degF":
            temp = (temp - 32) / 1.8
        elif temp_unit == "wmoUnit:K":
            temp -= 273.15
        elif temp_unit not in (None, "wmoUnit:degC"):
            temp = None
        if temp is not None and not math.isfinite(temp * 1.8 + 32):
            temp = None
    humidity, _ = quantity(properties, "relativeHumidity")
    if humidity is not None and not 0 <= humidity <= 100:
        humidity = None
    direction = direction % 360 if direction is not None and 0 <= direction <= 360 else None
    return {
        "station_id": station_id,
        "station_name": station_name,
        "wind_speed_mph": round(kmh / 1.609344, 1),
        "wind_speed_kmh": round(kmh, 1),
        "wind_direction_deg": round(direction, 1) if direction is not None else None,
        "wind_direction_cardinal": get_cardinal_direction(direction),
        "temperature_f": round(temp * 1.8 + 32, 1) if temp is not None else None,
        "temperature_c": round(temp, 1) if temp is not None else None,
        "relative_humidity": round(humidity, 1) if humidity is not None else None,
        "timestamp": properties.get("timestamp"),
    }


async def nws_json(client, url):
    res = await client.get(url)
    if res.status_code != 200:
        raise UpstreamError("Weather service returned an error status.")
    try:
        data = res.json()
    except ValueError as exc:
        raise UpstreamError("Weather service returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise UpstreamError("Weather service returned an invalid document.")
    return data


async def fetch_observation(point: str) -> LoadedData:
    """Network work for one cache miss: points -> stations -> up to three station observations."""
    async with httpx.AsyncClient(
        headers={"User-Agent": config.NWS_USER_AGENT}, timeout=10.0, follow_redirects=False
    ) as client:
        # Query the same rounded point used as the cache key.
        points = await nws_json(client, f"https://api.weather.gov/points/{point}")
        stations_url = mapping(points.get("properties")).get("observationStations")
        if not is_allowed_nws_url(stations_url):
            raise UpstreamError("Weather service returned an unexpected observation stations link.")
        stations = await nws_json(client, stations_url)
        features = stations.get("features")
        if not isinstance(features, list):
            raise UpstreamError("Weather service returned invalid observation stations.")
        if not features:
            raise UpstreamError("No nearby weather observation stations found.", status_code=404)
        for feature in features[:3]:
            props = mapping(mapping(feature).get("properties"))
            station_id = props.get("stationIdentifier")
            if not isinstance(station_id, str) or not station_id.isascii() or not station_id.isalnum():
                continue
            try:
                data = await nws_json(client, f"https://api.weather.gov/stations/{station_id}/observations/latest")
                result = parse_weather(
                    mapping(data.get("properties")), station_id, props.get("name", "Unknown Station")
                )
                observed = observation_time(result["timestamp"])
                return LoadedData(
                    result,
                    min(time.time() + 300, observed + config.MAX_OBSERVATION_AGE_SECS),
                    utc_stamp(min(time.time(), observed)),
                )
            except (UpstreamError, httpx.RequestError, ValueError, TypeError):
                logger.warning("Invalid/unavailable observation at station %s; trying next station.", station_id)
        raise UpstreamError("Failed to fetch valid meteorological observations from any nearby station.")


async def observation_snapshot(latitude: float, longitude: float) -> Snapshot:
    """Latest usable station observation near a point; shared by the route and the alert engine."""
    west, south, east, north = config.WEATHER_BOUNDS
    if not (south <= latitude <= north and west <= longitude <= east):
        raise HTTPException(status_code=400, detail="Coordinates must be within or near the Utah region boundary.")
    cache_key = f"{latitude:.3f},{longitude:.3f}"
    if cache_key not in weather_cache:
        # Never evict an in-flight entry, or a second request could start a duplicate refresh.
        if len(weather_cache) >= config.WEATHER_CACHE_LIMIT:
            oldest = next(
                (key for key, item in weather_cache.items() if not item.get("lock", asyncio.Lock()).locked()), None
            )
            if oldest is None:
                raise HTTPException(status_code=503, detail="Weather service is busy. Please retry.")
            del weather_cache[oldest]
        weather_cache[cache_key] = new_cache()
    entry = weather_cache[cache_key]

    async def load():
        # Wait briefly for a global slot, then fail fast: refresh() serves stale data if any and backs
        # off this point, instead of letting a flood of distinct points queue up behind NWS.
        try:
            async with asyncio.timeout(config.NWS_SLOT_WAIT_SECS):
                await nws_fetch_slots.acquire()
        except TimeoutError:
            raise UpstreamError("Weather service is busy. Please retry.", status_code=503) from None
        try:
            return await fetch_observation(cache_key)
        finally:
            nws_fetch_slots.release()

    return await refresh(entry, load, ttl=300)


@router.get("/api/weather")
async def get_weather(
    latitude: float = Query(..., description="Latitude coordinate"),
    longitude: float = Query(..., description="Longitude coordinate"),
    response: Response = None,
):
    return (await observation_snapshot(latitude, longitude)).send(response)
