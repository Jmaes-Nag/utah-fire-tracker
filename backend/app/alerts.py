"""Cities-at-risk alerts derived from incidents, wind, CAP warnings and municipal boundaries."""

import asyncio
import logging
import time
from copy import deepcopy
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException, Response
from shapely.affinity import scale, translate
from shapely.geometry import Point

from . import arcgis, boundaries, config, feeds
from . import weather as weather_feed
from .cache import LoadedData, cached, new_cache
from .geometry import UTAH_ENVELOPE, arcgis_polygon, threat_geometry
from .util import UpstreamError, finite_number, mapping, utc_stamp

logger = logging.getLogger("wildfire-tracker")
router = APIRouter()

alerts_cache = new_cache()


def cap_time(value):
    if value is None:
        return None
    number = finite_number(value)
    if number is not None:
        # ArcGIS date fields are epoch milliseconds, including dates prior to 2001.
        return number / 1000
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except ValueError:
            pass
    raise ValueError("Invalid CAP time")


def parse_cap(raw):
    parsed, malformed = [], False
    now = time.time()
    for feature in raw["features"]:
        feature = mapping(feature)
        attrs, geometry = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
        if str(attrs.get("status", "Actual")).lower() != "actual":
            continue
        if str(attrs.get("msgType", "Alert")).lower() not in {"alert", "update"}:
            continue
        try:
            expires, effective = cap_time(attrs.get("expires")), cap_time(attrs.get("effective"))
            if (expires is not None and expires <= now) or (effective is not None and effective > now):
                continue
            geom = arcgis_polygon(geometry.get("rings"))
            if geom.intersects(UTAH_ENVELOPE):
                parsed.append({"attributes": attrs, "geometry": geom, "rings": geometry["rings"]})
        except (ValueError, TypeError):
            malformed = True
    if malformed:
        # An unparseable live warning could cover Utah: preserve prior alerts rather than clear it.
        raise UpstreamError("CAP feed contains malformed active warnings.")
    return parsed


def merge_alerts(previous, current):
    """During outages retain known warnings while admitting new, observable risks."""
    combined = {item["city"]: deepcopy(item) for item in previous}
    for item in current:
        if item["city"] not in combined:
            combined[item["city"]] = deepcopy(item)
            continue
        existing = combined[item["city"]]
        if item["risk_level"] > existing.get("risk_level", 0):
            existing.update(risk_level=item["risk_level"], risk_level_text=item["risk_level_text"])
        for field in ("fires", "fema_alerts"):
            values = existing.setdefault(field, [])
            for value in item[field]:
                if value not in values:
                    values.append(deepcopy(value))
    return sorted(
        combined.values(),
        key=lambda item: (
            -item.get("risk_level", 0),
            min((fire["distance_miles"] for fire in item.get("fires", [])), default=999),
        ),
    )


@router.get("/api/alerts")
async def get_alerts(response: Response = None):
    async def load():
        if not boundaries.muni_boundaries:
            await boundaries.recover_boundaries()
        if not boundaries.muni_boundaries:
            raise UpstreamError("Utah municipal boundaries are unavailable.", status_code=503)
        incident_response = Response()
        try:
            incidents = await feeds.get_incidents(response=incident_response)
        except HTTPException as exc:
            raise UpstreamError("Incident data unavailable for alert evaluation.", status_code=503) from exc
        degraded = incident_response.headers.get("X-Data-Stale") == "true"
        dependencies = [feeds.incidents_cache] if feeds.incidents_cache["data"] is not None else []
        cap_failed = False
        try:
            raw = await arcgis.fetch_arcgis(
                config.CAP_URL,
                {
                    "where": "countryCode='us' AND (event LIKE '%Wildfire%' "
                    "OR event LIKE '%Evacuate%' OR event LIKE '%Evacuation%')",
                    "outFields": "*",
                    "outSR": "4326",
                    "f": "json",
                },
                timeout=10,
            )
            parsed_fema = parse_cap(raw)
        except (httpx.RequestError, UpstreamError, ValueError):
            logger.warning("CAP warnings unavailable; alert refresh is degraded.")
            cap_failed, parsed_fema = True, []
        semaphore = asyncio.Semaphore(8)

        async def fire_weather(incident):
            async with semaphore:
                weather_response = Response()
                try:
                    weather = await asyncio.wait_for(
                        weather_feed.get_weather(
                            latitude=incident["latitude"], longitude=incident["longitude"], response=weather_response
                        ),
                        timeout=12,
                    )
                    entry = weather_feed.weather_cache.get(f"{incident['latitude']:.3f},{incident['longitude']:.3f}")
                    if entry is not None:
                        dependencies.append(entry)
                    return weather, weather_response.headers.get("X-Data-Stale") == "true"
                except (TimeoutError, HTTPException):
                    return None, True

        weather_results = await asyncio.gather(*(fire_weather(incident) for incident in incidents))
        alerts = {}

        def city_alert(muni):
            return alerts.setdefault(
                muni["name"],
                {
                    "city": muni["name"],
                    "object_id": muni["object_id"],
                    "fips": muni["fips"],
                    "risk_level": 1,
                    "risk_level_text": "Ready",
                    "centroid": muni["centroid"],
                    "fires": [],
                    "fema_alerts": [],
                },
            )

        for incident, (weather, weather_stale) in zip(incidents, weather_results, strict=True):
            degraded = degraded or weather_stale
            speed = weather.get("wind_speed_mph", 0.0) if weather else 0.0
            direction = weather.get("wind_direction_deg") if weather else None
            cardinal = weather.get("wind_direction_cardinal", "N/A") if weather else "N/A"
            lat, lon = incident["latitude"], incident["longitude"]
            base, plume, miles_per_lon, miles_per_lat = threat_geometry(lat, lon, speed, direction)
            for muni in boundaries.muni_boundaries:
                if not plume.intersects(muni["geometry"]):
                    continue
                record = city_alert(muni)
                if base.intersects(muni["geometry"]):
                    record.update(risk_level=2, risk_level_text="Set")
                local_city = scale(
                    translate(muni["geometry"], xoff=-lon, yoff=-lat),
                    xfact=miles_per_lon,
                    yfact=miles_per_lat,
                    origin=(0, 0),
                )
                if not any(fire["id"] == incident["id"] for fire in record["fires"]):
                    record["fires"].append(
                        {
                            "name": incident["name"],
                            "id": incident["id"],
                            "distance_miles": round(Point(0, 0).distance(local_city), 1),
                            "wind_speed_mph": speed,
                            "wind_direction": cardinal,
                            "wind_direction_deg": direction,
                        }
                    )
        for muni in boundaries.muni_boundaries:
            for warning in parsed_fema:
                if warning["geometry"].intersects(muni["geometry"]):
                    record = city_alert(muni)
                    record.update(risk_level=2, risk_level_text="Set")
                    record["fema_alerts"].append(
                        {
                            **{
                                key: warning["attributes"].get(key)
                                for key in (
                                    "event",
                                    "headline",
                                    "description",
                                    "instruction",
                                    "severity",
                                    "urgency",
                                    "expires",
                                )
                            },
                            "rings": warning["rings"],
                        }
                    )
        result = sorted(
            alerts.values(),
            key=lambda item: (
                -item["risk_level"],
                min((fire["distance_miles"] for fire in item["fires"]), default=999),
            ),
        )
        if degraded or cap_failed:
            if alerts_cache["data"] is not None:
                result = merge_alerts(alerts_cache["data"], result)
            raise UpstreamError("Alert dependencies are unavailable or stale.", status_code=503, partial=result)
        completed = time.time()
        # A derived alert must never appear newer, or stay fresh longer, than its inputs.
        return LoadedData(
            result,
            min([completed + config.CACHE_DURATION_SECS] + [entry["expiry"] for entry in dependencies]),
            min([utc_stamp(completed)] + [entry["created_at"] for entry in dependencies if entry["created_at"]]),
        )

    return await cached(alerts_cache, load, response)
