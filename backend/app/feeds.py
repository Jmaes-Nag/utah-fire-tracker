"""Map feeds: incidents, perimeters, hotspots, smoke and PM2.5 monitors."""

import logging
import math
import time

import httpx
from fastapi import APIRouter, Response

from . import arcgis, config
from .cache import LoadedData, cached, new_cache
from .geometry import UTAH_ENVELOPE, arcgis_polygon, polygon_geometry, utah_point
from .util import UpstreamError, finite_number, mapping, utc_stamp

logger = logging.getLogger("wildfire-tracker")
router = APIRouter()

incidents_cache = new_cache()
perimeters_cache = new_cache()
hotspots_cache = new_cache()
smoke_cache = new_cache()
aqi_cache = new_cache()


def require_usable(features, cleaned):
    if features and not cleaned:
        raise UpstreamError("Upstream feed contained no usable records.")
    return cleaned


def optional_snapshot(features, cleaned, payload=None):
    """Keep usable map features while disclosing omissions on every cached response."""
    require_usable(features, cleaned)
    data = cleaned if payload is None else payload
    if len(features) == len(cleaned):
        return data
    now = time.time()
    return LoadedData(data, now + config.CACHE_DURATION_SECS, utc_stamp(now), incomplete=True)


def active_wildfire(attributes, prefix=""):
    # NIFC: WF=wildfire, RX=prescribed fire, CX=incident complex.
    # Retain complexes and unknown categories conservatively.
    category = attributes.get(prefix + "IncidentTypeCategory")
    if isinstance(category, str) and category.strip().upper() == "RX":
        return False
    out = finite_number(attributes.get(prefix + "FireOutDateTime"))
    return out is None or out <= 0


@router.get("/api/incidents")
async def get_incidents(response: Response = None):
    async def load():
        raw = await arcgis.fetch_arcgis(
            config.NIFC_API_URL,
            {"where": "POOState='US-UT' OR POOState='UT'", "outFields": "*", "outSR": "4326", "f": "json"},
        )
        cleaned = []
        malformed = False
        for feature in raw["features"]:
            feature = mapping(feature)
            attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
            if not active_wildfire(attrs):
                continue
            point = utah_point(geom.get("x"), geom.get("y"))
            if not point or attrs.get("POOState", "UT") not in {"UT", "US-UT"}:
                malformed = True
                continue
            cleaned.append(
                {
                    "id": attrs.get("UniqueFireIdentifier") or str(attrs.get("OBJECTID")),
                    "object_id": attrs.get("OBJECTID"),
                    "name": attrs.get("IncidentName") or "Unknown Wildfire",
                    "acres": finite_number(
                        attrs.get("IncidentSize") or attrs.get("DailyAcres") or attrs.get("CalculatedAcres")
                    )
                    or 0.0,
                    "containment": finite_number(attrs.get("PercentContained")),
                    "discovered": attrs.get("FireDiscoveryDateTime") or attrs.get("IncidentStartDate"),
                    "cause": attrs.get("FireCause") or attrs.get("FireCauseGeneral") or "Undetermined",
                    "county": attrs.get("POOCounty") or "Unknown County",
                    "latitude": point[1],
                    "longitude": point[0],
                }
            )
        if malformed:
            raise UpstreamError("Incident feed contains unusable records; refusing an incomplete snapshot.")
        return cleaned

    return await cached(incidents_cache, load, response)


@router.get("/api/perimeters")
async def get_perimeters(response: Response = None):
    async def load():
        raw = await arcgis.fetch_arcgis(
            config.PERIMETERS_URL,
            {"where": "attr_POOState='US-UT' OR attr_POOState='UT'", "outFields": "*", "outSR": "4326", "f": "geojson"},
            timeout=30,
        )
        cleaned = []
        eligible = []
        for feature in raw["features"]:
            if not active_wildfire(mapping(mapping(feature).get("properties")), prefix="attr_"):
                continue
            eligible.append(feature)
            try:
                geom = polygon_geometry(mapping(feature).get("geometry"))
                if geom.intersects(UTAH_ENVELOPE):
                    cleaned.append({**feature, "geometry": geom.__geo_interface__})
            except (ValueError, TypeError, KeyError):
                continue
        return optional_snapshot(eligible, cleaned, {"type": "FeatureCollection", "features": cleaned})

    return await cached(perimeters_cache, load, response)


@router.get("/api/hotspots")
async def get_hotspots(response: Response = None):
    async def load():
        raw = await arcgis.fetch_arcgis(
            config.HOTSPOTS_URL, arcgis.spatial_params("FID,Lon,Lat,YearDay,Time,Satellite,Method,FRP"), timeout=30
        )
        cleaned = []
        for feature in raw["features"]:
            feature = mapping(feature)
            attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
            lon = geom.get("x") if geom.get("x") is not None else attrs.get("Lon")
            lat = geom.get("y") if geom.get("y") is not None else attrs.get("Lat")
            point = utah_point(lon, lat)
            if point:
                cleaned.append(
                    {
                        "fid": attrs.get("FID"),
                        "latitude": point[1],
                        "longitude": point[0],
                        "satellite": attrs.get("Satellite", "Unknown"),
                        "method": attrs.get("Method", "Unknown"),
                        "frp": finite_number(attrs.get("FRP")) or 0.0,
                        "time": attrs.get("Time", ""),
                        "yearday": attrs.get("YearDay", ""),
                    }
                )
        return optional_snapshot(raw["features"], cleaned)

    return await cached(hotspots_cache, load, response)


@router.get("/api/smoke")
async def get_smoke(response: Response = None):
    async def load():
        params = arcgis.spatial_params("FID,Satellite,Start,End_,Density")
        for index, url in enumerate((config.SMOKE_URL, config.SMOKE_FALLBACK_URL)):
            try:
                raw = await arcgis.fetch_arcgis(url, params)
                cleaned = []
                for feature in raw["features"]:
                    feature = mapping(feature)
                    attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
                    try:
                        if not arcgis_polygon(geom.get("rings")).intersects(UTAH_ENVELOPE):
                            continue
                    except (ValueError, TypeError):
                        continue
                    cleaned.append(
                        {
                            "fid": attrs.get("FID") if attrs.get("FID") is not None else attrs.get("OBJECTID"),
                            "satellite": attrs.get("Satellite"),
                            "start": attrs.get("Start"),
                            "end": attrs.get("End_"),
                            "density": attrs.get("Density", "Light"),
                            "rings": geom["rings"],
                        }
                    )
                return optional_snapshot(raw["features"], cleaned)
            except (UpstreamError, httpx.RequestError, ValueError):
                if index:
                    raise
                logger.warning("Primary smoke service failed; trying replica.")

    return await cached(smoke_cache, load, response)


def calculate_pm25_aqi(pm25: float) -> int:
    """EPA 2024 PM2.5 breakpoints; truncate concentration to one decimal place."""
    pm25 = finite_number(pm25)
    if pm25 is None or pm25 < 0:
        return 0
    if pm25 >= 325.4:
        return 500
    pm25 = math.floor(pm25 * 10) / 10
    for low, high, index_low, index_high in (
        (0, 9.0, 0, 50),
        (9.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200),
        (125.5, 225.4, 201, 300),
        (225.5, 325.4, 301, 500),
    ):
        if pm25 <= high:
            return math.floor((index_high - index_low) / (high - low) * (pm25 - low) + index_low + 0.5)
    return 500


@router.get("/api/aqi")
async def get_aqi(response: Response = None):
    async def load():
        raw = await arcgis.fetch_arcgis(
            config.AQI_URL,
            arcgis.spatial_params(
                "OBJECTID,location_id,city,location,lastUpdated,value,unit,provider_name,instrument_name",
                "country='US'",
            ),
        )
        cleaned = []
        tiers = [
            (50, "#10b981", "Good"),
            (100, "#eab308", "Moderate"),
            (150, "#f97316", "Unhealthy for Sensitive Groups"),
            (200, "#ef4444", "Unhealthy"),
            (300, "#a855f7", "Very Unhealthy"),
            (500, "#7f1d1d", "Hazardous"),
        ]
        for feature in raw["features"]:
            feature = mapping(feature)
            attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
            point = utah_point(geom.get("x"), geom.get("y"))
            pm25 = finite_number(attrs.get("value"))
            if not point or pm25 is None or pm25 < 0:
                continue
            unit = attrs.get("unit")
            if unit is not None and unit not in {"µg/m³", "μg/m³", "ug/m3", "µg/m3", "μg/m3", "ug/m^3"}:
                continue
            aqi = calculate_pm25_aqi(pm25)
            _, color, tier = next(item for item in tiers if aqi <= item[0])
            cleaned.append(
                {
                    "id": attrs.get("location_id") or attrs.get("OBJECTID"),
                    "city": attrs.get("city") or attrs.get("location", "Unknown Location"),
                    "name": attrs.get("location", "Unknown Monitor"),
                    "pm25": pm25,
                    "aqi": aqi,
                    "tier": tier,
                    "color": color,
                    "latitude": point[1],
                    "longitude": point[0],
                    "provider": attrs.get("provider_name", "Government Monitor"),
                    "last_updated": attrs.get("lastUpdated"),
                }
            )
        return optional_snapshot(raw["features"], cleaned)

    return await cached(aqi_cache, load, response)
