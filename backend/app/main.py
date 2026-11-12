import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
from shapely import make_valid
from shapely.errors import GEOSException
from shapely.affinity import scale, translate
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import unary_union

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wildfire-tracker")


@asynccontextmanager
async def lifespan(app):
    await startup_event()
    task = asyncio.create_task(boundary_recovery_loop()) if not muni_boundaries else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Utah Wildfire and Wind Tracking API", lifespan=lifespan)
muni_boundaries: List[Dict[str, Any]] = []
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=origins, allow_credentials=origins != ["*"],
    allow_methods=["GET"], allow_headers=["*"],
    expose_headers=["X-Data-Timestamp", "X-Data-Stale"],
)

NWS_USER_AGENT = os.getenv("NWS_USER_AGENT", "UtahWildfireTracker/1.0 (contact@example.com)")
NIFC_API_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"
PERIMETERS_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
HOTSPOTS_URL = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Fire_Detections_(v1)/FeatureServer/0/query"
SMOKE_URL = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0/query"
SMOKE_FALLBACK_URL = "https://services2.arcgis.com/r6iFVcMJeA4kB4GC/arcgis/rest/services/NOAA_HMS_Smoke_Detection_Replica/FeatureServer/0/query"
AQI_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/Air_Quality_PM25_Latest_Results/FeatureServer/0/query"
CAP_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/CAP_Alerts_Feed/FeatureServer/0/query"
BOUNDARIES_URL = "https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/UtahMunicipalBoundaries/FeatureServer/0/query"
BOUNDARIES_PATH = Path(__file__).resolve().parent / "data" / "utah_municipal_boundaries.geojson"
ALLOWED_NWS_HOSTS = {"api.weather.gov"}
UTAH_ENVELOPE = box(-114.05, 37.0, -109.05, 42.0)
CACHE_DURATION_SECS = 15 * 60
RETRY_BACKOFF_SECS = 30
BOUNDARY_MAX_BACKOFF_SECS = CACHE_DURATION_SECS
BOUNDARY_MAX_BACKGROUND_ATTEMPTS = 5
WEATHER_CACHE_LIMIT = 1000
MAX_OBSERVATION_AGE_SECS = 2 * 60 * 60


def new_cache():
    return {"data": None, "expiry": 0.0, "created_at": ""}


def new_boundary_state():
    return {"retry_after": 0.0, "failures": 0}


cache = new_cache()
perimeter_cache = new_cache()
hotspots_cache = new_cache()
smoke_cache = new_cache()
aqi_cache = new_cache()
alerts_cache = new_cache()
boundary_state = new_boundary_state()
weather_cache: Dict[str, Any] = {}


class UpstreamError(Exception):
    def __init__(self, detail="Upstream service returned invalid data.", status_code=502, partial=None):
        super().__init__(detail)
        self.status_code = status_code
        self.partial = partial


@dataclass
class LoadedData:
    data: Any
    expiry: float
    created_at: str
    incomplete: bool = False


def set_data_headers(response, entry, stale=False):
    if response is not None:
        response.headers["X-Data-Timestamp"] = entry.get("created_at", "")
        response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp, X-Data-Stale"
        if stale:
            response.headers["X-Data-Stale"] = "true"
        elif "X-Data-Stale" in response.headers:
            del response.headers["X-Data-Stale"]


async def cached(entry, loader, response=None, ttl=CACHE_DURATION_SECS):
    """Coalesce refreshes, including failed refreshes; never renew stale data's age."""
    lock = entry.setdefault("lock", asyncio.Lock())
    async with lock:
        now = time.time()
        if entry["data"] is not None and now < entry["expiry"]:
            set_data_headers(response, entry, stale=entry.get("incomplete", False))
            return entry["data"]
        if now >= entry.get("retry_after", 0):
            try:
                data = await loader()
            except (UpstreamError, httpx.RequestError, ValueError, TypeError, OverflowError, GEOSException) as exc:
                logger.warning("Feed refresh failed: %s", exc)
                entry["failure"] = exc if isinstance(exc, UpstreamError) else UpstreamError(
                    "Unable to connect to upstream service." if isinstance(exc, httpx.RequestError)
                    else "Upstream service returned invalid data.",
                    status_code=503 if isinstance(exc, httpx.RequestError) else 502,
                )
                entry["retry_after"] = time.time() + RETRY_BACKOFF_SECS
                entry["failure_created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            else:
                completed = time.time()
                expiry = completed + ttl
                created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed))
                incomplete = isinstance(data, LoadedData) and data.incomplete
                if isinstance(data, LoadedData):
                    expiry, created_at, data = min(expiry, data.expiry), data.created_at, data.data
                entry.update(data=data, expiry=expiry, created_at=created_at, incomplete=incomplete)
                entry.pop("failure", None)
                entry.pop("retry_after", None)
                set_data_headers(response, entry, stale=incomplete or completed >= expiry)
                return data
        if entry["data"] is not None:
            set_data_headers(response, entry, stale=True)
            failure = entry.get("failure")
            return failure.partial if failure and failure.partial is not None else entry["data"]
        failure = entry["failure"]
        if failure.partial is not None:
            # Cold-start degraded alerts are visible but never cached as a fresh all-clear.
            set_data_headers(response, {"created_at": entry["failure_created_at"]}, stale=True)
            return failure.partial
        raise HTTPException(status_code=failure.status_code, detail=str(failure))


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def coordinates(lon, lat):
    lon, lat = finite_number(lon), finite_number(lat)
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def utah_point(lon, lat):
    point = coordinates(lon, lat)
    return point if point and UTAH_ENVELOPE.covers(Point(*point)) else None


def polygon_geometry(geometry):
    """Validate/repair polygonal GeoJSON without allowing NaN or non-WGS84 coordinates."""
    if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Expected polygon geometry")
    polygons = geometry.get("coordinates")
    polygons = [polygons] if geometry["type"] == "Polygon" else polygons
    if not isinstance(polygons, list) or not polygons:
        raise ValueError("Empty polygon")
    for rings in polygons:
        if not isinstance(rings, list) or not rings:
            raise ValueError("Missing polygon rings")
        for ring in rings:
            if not isinstance(ring, list) or len(ring) < 4:
                raise ValueError("Invalid polygon ring")
            for point in ring:
                if (not isinstance(point, (list, tuple)) or len(point) not in (2, 3)
                        or coordinates(*point[:2]) is None or any(finite_number(value) is None for value in point)):
                    raise ValueError("Invalid polygon coordinate")
            if ring[0][:2] != ring[-1][:2]:
                raise ValueError("Unclosed polygon ring")
    try:
        geom = shape(geometry)
        if not geom.is_valid:
            geom = make_valid(geom)
    except GEOSException as exc:
        raise ValueError("Invalid polygon geometry") from exc
    if geom.geom_type == "GeometryCollection":
        geom = unary_union([g for g in geom.geoms if g.geom_type in {"Polygon", "MultiPolygon"}])
    if geom.is_empty or geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.area <= 0:
        raise ValueError("Degenerate polygon")
    return geom


def arcgis_polygon(rings):
    """ArcGIS rings may be separate shells, holes, or nested islands (even-odd fill)."""
    if not isinstance(rings, list) or not rings:
        raise ValueError("Missing ArcGIS rings")
    result = None
    for ring in rings:
        poly = polygon_geometry({"type": "Polygon", "coordinates": [ring]})
        result = poly if result is None else result.symmetric_difference(poly)
    if result.is_empty or not result.is_valid:
        raise ValueError("Invalid ArcGIS polygon")
    return result


def feature_list(payload, geojson=False):
    if not isinstance(payload, dict) or "error" in payload:
        raise UpstreamError("ArcGIS service returned an error or invalid document.")
    if geojson and payload.get("type") != "FeatureCollection":
        raise UpstreamError("ArcGIS service returned invalid GeoJSON.")
    if not isinstance(payload.get("features"), list):
        raise UpstreamError("ArcGIS service did not return a feature list.")
    return payload["features"]


async def fetch_arcgis(url, params, timeout=15.0):
    """Read every page or reject the refresh; truncated feeds can suppress alerts."""
    features, fingerprints = [], set()
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(100):
            page_params = dict(params)
            if features:
                page_params["resultOffset"] = len(features)
            res = await client.get(url, params=page_params)
            if res.status_code != 200:
                raise UpstreamError("ArcGIS service returned an error status.")
            try:
                raw = res.json()
            except ValueError as exc:
                raise UpstreamError("ArcGIS service returned invalid JSON.") from exc
            page = feature_list(raw, geojson=params.get("f") == "geojson")
            fingerprint = json.dumps(page, sort_keys=True, allow_nan=False)
            has_more = raw.get("exceededTransferLimit") or mapping(raw.get("properties")).get("exceededTransferLimit")
            if fingerprint in fingerprints or (has_more and not page):
                raise UpstreamError("ArcGIS pagination did not advance.")
            fingerprints.add(fingerprint)
            features.extend(page)
            if not has_more:
                return {"type": "FeatureCollection", "features": features}
    raise UpstreamError("ArcGIS pagination exceeded the safety limit.")


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
    return LoadedData(data, now + CACHE_DURATION_SECS,
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), incomplete=True)


def spatial_params(fields, where="1=1"):
    return {"where": where, "geometryType": "esriGeometryEnvelope",
            "geometry": "-114.05,37.0,-109.05,42.0", "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326", "outFields": fields, "f": "json"}


def active_wildfire(attributes, prefix=""):
    # NIFC: WF=wildfire, RX=prescribed fire, CX=incident complex.
    # Retain complexes and unknown categories conservatively.
    category = attributes.get(prefix + "IncidentTypeCategory")
    if isinstance(category, str) and category.strip().upper() == "RX":
        return False
    out = finite_number(attributes.get(prefix + "FireOutDateTime"))
    return out is None or out <= 0


def is_allowed_nws_url(url: Optional[str]) -> bool:
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        return bool(parsed and parsed.scheme == "https" and parsed.hostname in ALLOWED_NWS_HOSTS
                    and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        return False


def get_cardinal_direction(degrees: Optional[float]) -> str:
    degrees = finite_number(degrees)
    if degrees is None:
        return "N/A"
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return directions[int((degrees % 360 + 11.25) / 22.5) % 16]


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "time": time.time()}


@app.get("/api/incidents")
async def get_incidents(response: Response = None):
    async def load():
        raw = await fetch_arcgis(NIFC_API_URL, {
            "where": "POOState='US-UT' OR POOState='UT'", "outFields": "*", "outSR": "4326", "f": "json"})
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
            cleaned.append({
                "id": attrs.get("UniqueFireIdentifier") or str(attrs.get("OBJECTID")),
                "object_id": attrs.get("OBJECTID"), "name": attrs.get("IncidentName") or "Unknown Wildfire",
                "acres": finite_number(attrs.get("IncidentSize") or attrs.get("DailyAcres") or attrs.get("CalculatedAcres")) or 0.0,
                "containment": finite_number(attrs.get("PercentContained")),
                "discovered": attrs.get("FireDiscoveryDateTime") or attrs.get("IncidentStartDate"),
                "cause": attrs.get("FireCause") or attrs.get("FireCauseGeneral") or "Undetermined",
                "county": attrs.get("POOCounty") or "Unknown County", "latitude": point[1], "longitude": point[0],
            })
        if malformed:
            raise UpstreamError("Incident feed contains unusable records; refusing an incomplete snapshot.")
        return cleaned
    return await cached(cache, load, response)


@app.get("/api/perimeters")
async def get_perimeters(response: Response = None):
    async def load():
        raw = await fetch_arcgis(PERIMETERS_URL, {
            "where": "attr_POOState='US-UT' OR attr_POOState='UT'", "outFields": "*", "outSR": "4326", "f": "geojson"}, timeout=30)
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
    return await cached(perimeter_cache, load, response)


@app.get("/api/hotspots")
async def get_hotspots(response: Response = None):
    async def load():
        raw = await fetch_arcgis(HOTSPOTS_URL, spatial_params("FID,Lon,Lat,YearDay,Time,Satellite,Method,FRP"), timeout=30)
        cleaned = []
        for feature in raw["features"]:
            feature = mapping(feature)
            attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
            lon = geom.get("x") if geom.get("x") is not None else attrs.get("Lon")
            lat = geom.get("y") if geom.get("y") is not None else attrs.get("Lat")
            point = utah_point(lon, lat)
            if point:
                cleaned.append({"fid": attrs.get("FID"), "latitude": point[1], "longitude": point[0],
                                "satellite": attrs.get("Satellite", "Unknown"), "method": attrs.get("Method", "Unknown"),
                                "frp": finite_number(attrs.get("FRP")) or 0.0,
                                "time": attrs.get("Time", ""), "yearday": attrs.get("YearDay", "")})
        return optional_snapshot(raw["features"], cleaned)
    return await cached(hotspots_cache, load, response)


@app.get("/api/smoke")
async def get_smoke(response: Response = None):
    async def load():
        params = spatial_params("FID,Satellite,Start,End_,Density")
        for index, url in enumerate((SMOKE_URL, SMOKE_FALLBACK_URL)):
            try:
                raw = await fetch_arcgis(url, params)
                cleaned = []
                for feature in raw["features"]:
                    feature = mapping(feature)
                    attrs, geom = mapping(feature.get("attributes")), mapping(feature.get("geometry"))
                    try:
                        if not arcgis_polygon(geom.get("rings")).intersects(UTAH_ENVELOPE):
                            continue
                    except (ValueError, TypeError):
                        continue
                    cleaned.append({"fid": attrs.get("FID") if attrs.get("FID") is not None else attrs.get("OBJECTID"),
                                    "satellite": attrs.get("Satellite"), "start": attrs.get("Start"), "end": attrs.get("End_"),
                                    "density": attrs.get("Density", "Light"), "rings": geom["rings"]})
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
        (0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200), (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500),
    ):
        if pm25 <= high:
            return math.floor((index_high - index_low) / (high - low) * (pm25 - low) + index_low + 0.5)
    return 500


@app.get("/api/aqi")
async def get_aqi(response: Response = None):
    async def load():
        raw = await fetch_arcgis(AQI_URL, spatial_params(
            "OBJECTID,location_id,city,location,lastUpdated,value,unit,provider_name,instrument_name", "country='US'"))
        cleaned = []
        tiers = [(50, "#10b981", "Good"), (100, "#eab308", "Moderate"),
                 (150, "#f97316", "Unhealthy for Sensitive Groups"), (200, "#ef4444", "Unhealthy"),
                 (300, "#a855f7", "Very Unhealthy"), (500, "#7f1d1d", "Hazardous")]
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
            cleaned.append({"id": attrs.get("location_id") or attrs.get("OBJECTID"),
                            "city": attrs.get("city") or attrs.get("location", "Unknown Location"),
                            "name": attrs.get("location", "Unknown Monitor"), "pm25": pm25, "aqi": aqi,
                            "tier": tier, "color": color, "latitude": point[1], "longitude": point[0],
                            "provider": attrs.get("provider_name", "Government Monitor"), "last_updated": attrs.get("lastUpdated")})
        return optional_snapshot(raw["features"], cleaned)
    return await cached(aqi_cache, load, response)


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
    if not -300 <= age <= MAX_OBSERVATION_AGE_SECS:
        raise ValueError("Observation is too old or in the future")
    return timestamp


def parse_weather(properties, station_id, station_name):
    observation_time(properties.get("timestamp"))
    speed, unit = quantity(properties, "windSpeed")
    direction, _ = quantity(properties, "windDirection")
    if speed is None or speed < 0 or (speed > 0 and (direction is None or not 0 <= direction <= 360)):
        raise ValueError("Missing or invalid wind measurements")
    conversions = {"wmoUnit:km_h-1": 1.0, "wmoUnit:m_s-1": 3.6,
                   "wmoUnit:mi_h-1": 1.609344, "wmoUnit:kn": 1.852,
                   "wmoUnit:kt": 1.852}
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
    return {"station_id": station_id, "station_name": station_name,
            "wind_speed_mph": round(kmh / 1.609344, 1), "wind_speed_kmh": round(kmh, 1),
            "wind_direction_deg": round(direction, 1) if direction is not None else None,
            "wind_direction_cardinal": get_cardinal_direction(direction),
            "temperature_f": round(temp * 1.8 + 32, 1) if temp is not None else None,
            "temperature_c": round(temp, 1) if temp is not None else None,
            "relative_humidity": round(humidity, 1) if humidity is not None else None,
            "timestamp": properties.get("timestamp")}


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


@app.get("/api/weather")
async def get_weather(
    latitude: float = Query(..., description="Latitude coordinate"),
    longitude: float = Query(..., description="Longitude coordinate"),
    response: Response = None,
):
    if not (35.0 <= latitude <= 45.0 and -116.0 <= longitude <= -107.0):
        raise HTTPException(status_code=400, detail="Coordinates must be within or near the Utah region boundary.")
    cache_key = f"{latitude:.3f},{longitude:.3f}"
    if cache_key not in weather_cache:
        # Never evict an in-flight entry, or a second request could start a duplicate refresh.
        if len(weather_cache) >= WEATHER_CACHE_LIMIT:
            oldest = next((key for key, item in weather_cache.items()
                           if not item.get("lock", asyncio.Lock()).locked()), None)
            if oldest is None:
                raise HTTPException(status_code=503, detail="Weather service is busy. Please retry.")
            del weather_cache[oldest]
        weather_cache[cache_key] = new_cache()
    entry = weather_cache[cache_key]

    async def load():
        async with httpx.AsyncClient(headers={"User-Agent": NWS_USER_AGENT}, timeout=10.0, follow_redirects=False) as client:
            # Query the same rounded point used as the cache key.
            points = await nws_json(client, f"https://api.weather.gov/points/{cache_key}")
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
                    result = parse_weather(mapping(data.get("properties")), station_id, props.get("name", "Unknown Station"))
                    observed = observation_time(result["timestamp"])
                    return LoadedData(result, min(time.time() + 300, observed + MAX_OBSERVATION_AGE_SECS),
                                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(time.time(), observed))))
                except (UpstreamError, httpx.RequestError, ValueError, TypeError):
                    logger.warning("Invalid/unavailable observation at station %s; trying next station.", station_id)
            raise UpstreamError("Failed to fetch valid meteorological observations from any nearby station.")
    return await cached(entry, load, response, ttl=300)


def parse_boundaries(data):
    features = feature_list(data, geojson=True)
    result, malformed = [], False
    for feature in features:
        feature = mapping(feature)
        try:
            geom = polygon_geometry(feature.get("geometry"))
            if not geom.intersects(UTAH_ENVELOPE):
                malformed = True
                continue
            props = mapping(feature.get("properties"))
            if not isinstance(props.get("NAME"), str) or not props["NAME"].strip():
                raise ValueError("Municipality name is missing or invalid")
            result.append({"name": props.get("NAME") or "Unknown", "object_id": props.get("OBJECTID"),
                           "fips": props.get("FIPS"), "geometry": geom,
                           "centroid": (geom.centroid.y, geom.centroid.x)})
        except (ValueError, TypeError, KeyError):
            malformed = True
    if malformed:
        raise ValueError("Incomplete municipal boundary snapshot")
    if not result:
        raise ValueError("No valid Utah municipal boundaries")
    return result


def persist_boundaries(data):
    """Persist validated boundaries atomically; never leave a truncated cache file."""
    BOUNDARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=BOUNDARIES_PATH.parent, delete=False) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, BOUNDARIES_PATH)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def recover_boundaries(force: bool = False) -> bool:
    """Single-flight SGID boundary recovery with bounded exponential backoff."""
    global muni_boundaries
    lock = boundary_state.setdefault("lock", asyncio.Lock())
    async with lock:
        if not force and muni_boundaries:
            return True
        now = time.time()
        if not force and now < boundary_state.get("retry_after", 0.0):
            return bool(muni_boundaries)
        try:
            data = await fetch_arcgis(BOUNDARIES_URL, {
                "where": "1=1", "outFields": "NAME,OBJECTID,FIPS", "outSR": "4326", "f": "geojson"}, timeout=60)
            loaded = parse_boundaries(data)
        except (httpx.RequestError, UpstreamError, ValueError):
            failures = boundary_state.get("failures", 0) + 1
            backoff = min(RETRY_BACKOFF_SECS * (2 ** (failures - 1)), BOUNDARY_MAX_BACKOFF_SECS)
            boundary_state.update(failures=failures, retry_after=time.time() + backoff)
            logger.exception("Municipal boundaries unavailable; alerts will report service unavailable.")
            return False
        try:
            persist_boundaries(data)
        except OSError:
            # A read-only disk should not discard a valid download from this process's memory.
            logger.exception("Could not persist municipal boundaries; using validated data in memory.")
        muni_boundaries = loaded
        boundary_state.update(failures=0, retry_after=0.0)
        logger.info("Loaded %d Utah municipal boundaries.", len(loaded))
        return True


async def boundary_recovery_loop(max_attempts: int = BOUNDARY_MAX_BACKGROUND_ATTEMPTS) -> bool:
    """Bounded background recovery when initial startup boundary load failed."""
    for _ in range(max_attempts):
        if muni_boundaries:
            return True
        delay = max(0.0, boundary_state.get("retry_after", 0.0) - time.time())
        if delay > 0:
            await asyncio.sleep(delay)
        if await recover_boundaries():
            return True
    return bool(muni_boundaries)


async def startup_event():
    global muni_boundaries
    try:
        with BOUNDARIES_PATH.open(encoding="utf-8") as handle:
            loaded = parse_boundaries(json.load(handle))
    except (OSError, ValueError, UpstreamError):
        logger.warning("Municipal boundary file missing/invalid; attempting SGID recovery.")
        await recover_boundaries(force=True)
        return
    muni_boundaries = loaded
    boundary_state.update(failures=0, retry_after=0.0)
    logger.info("Loaded %d Utah municipal boundaries.", len(loaded))


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
                parsed = parsed.replace(tzinfo=timezone.utc)
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


def threat_geometry(lat, lon, speed, direction):
    """Local miles projection corrects longitude scale and fills the complete wind corridor."""
    miles_per_lat = 69.0
    miles_per_lon = miles_per_lat * math.cos(math.radians(lat))
    speed = finite_number(speed) or 0.0
    direction = finite_number(direction)
    dx = dy = 0.0
    if direction is not None and speed > 0:
        downwind = math.radians((direction + 180) % 360)
        dx, dy = math.sin(downwind) * speed * 0.2, math.cos(downwind) * speed * 0.2
    # Operate in local miles, then map back to WGS84 for municipality overlay.
    base = Point(0, 0).buffer(5.0)
    plume = (LineString([(0, 0), (dx, dy)]) if dx or dy else Point(0, 0)).buffer(5.0)
    def to_geo(geom):
        return translate(scale(geom, xfact=1 / miles_per_lon, yfact=1 / miles_per_lat, origin=(0, 0)), xoff=lon, yoff=lat)
    return to_geo(base), to_geo(plume), miles_per_lon, miles_per_lat


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
    return sorted(combined.values(), key=lambda item: (
        -item.get("risk_level", 0), min((fire["distance_miles"] for fire in item.get("fires", [])), default=999)))


@app.get("/api/alerts")
async def get_alerts(response: Response = None):
    async def load():
        if not muni_boundaries:
            await recover_boundaries()
        if not muni_boundaries:
            raise UpstreamError("Utah municipal boundaries are unavailable.", status_code=503)
        incident_response = Response()
        try:
            incidents = await get_incidents(response=incident_response)
        except HTTPException as exc:
            raise UpstreamError("Incident data unavailable for alert evaluation.", status_code=503) from exc
        degraded = incident_response.headers.get("X-Data-Stale") == "true"
        dependencies = [cache] if cache["data"] is not None else []
        cap_failed = False
        try:
            raw = await fetch_arcgis(CAP_URL, {
                "where": "countryCode='us' AND (event LIKE '%Wildfire%' OR event LIKE '%Evacuate%' OR event LIKE '%Evacuation%')",
                "outFields": "*", "outSR": "4326", "f": "json"}, timeout=10)
            parsed_fema = parse_cap(raw)
        except (httpx.RequestError, UpstreamError, ValueError):
            logger.warning("CAP warnings unavailable; alert refresh is degraded.")
            cap_failed, parsed_fema = True, []
        semaphore = asyncio.Semaphore(8)

        async def fire_weather(incident):
            async with semaphore:
                weather_response = Response()
                try:
                    weather = await asyncio.wait_for(get_weather(
                        latitude=incident["latitude"], longitude=incident["longitude"], response=weather_response), timeout=12)
                    entry = weather_cache.get(f'{incident["latitude"]:.3f},{incident["longitude"]:.3f}')
                    if entry is not None:
                        dependencies.append(entry)
                    return weather, weather_response.headers.get("X-Data-Stale") == "true"
                except (HTTPException, asyncio.TimeoutError):
                    return None, True

        weather_results = await asyncio.gather(*(fire_weather(incident) for incident in incidents))
        alerts = {}

        def city_alert(muni):
            return alerts.setdefault(muni["name"], {
                "city": muni["name"], "object_id": muni["object_id"], "fips": muni["fips"],
                "risk_level": 1, "risk_level_text": "Ready", "centroid": muni["centroid"],
                "fires": [], "fema_alerts": [],
            })

        for incident, (weather, weather_stale) in zip(incidents, weather_results):
            degraded = degraded or weather_stale
            speed = weather.get("wind_speed_mph", 0.0) if weather else 0.0
            direction = weather.get("wind_direction_deg") if weather else None
            cardinal = weather.get("wind_direction_cardinal", "N/A") if weather else "N/A"
            lat, lon = incident["latitude"], incident["longitude"]
            base, plume, miles_per_lon, miles_per_lat = threat_geometry(lat, lon, speed, direction)
            for muni in muni_boundaries:
                if not plume.intersects(muni["geometry"]):
                    continue
                record = city_alert(muni)
                if base.intersects(muni["geometry"]):
                    record.update(risk_level=2, risk_level_text="Set")
                local_city = scale(translate(muni["geometry"], xoff=-lon, yoff=-lat),
                                   xfact=miles_per_lon, yfact=miles_per_lat, origin=(0, 0))
                if not any(fire["id"] == incident["id"] for fire in record["fires"]):
                    record["fires"].append({"name": incident["name"], "id": incident["id"],
                                            "distance_miles": round(Point(0, 0).distance(local_city), 1),
                                            "wind_speed_mph": speed, "wind_direction": cardinal, "wind_direction_deg": direction})
        for muni in muni_boundaries:
            for warning in parsed_fema:
                if warning["geometry"].intersects(muni["geometry"]):
                    record = city_alert(muni)
                    record.update(risk_level=2, risk_level_text="Set")
                    record["fema_alerts"].append({**{key: warning["attributes"].get(key) for key in (
                        "event", "headline", "description", "instruction", "severity", "urgency", "expires")}, "rings": warning["rings"]})
        result = sorted(alerts.values(), key=lambda item: (
            -item["risk_level"], min((fire["distance_miles"] for fire in item["fires"]), default=999)))
        if degraded or cap_failed:
            if alerts_cache["data"] is not None:
                result = merge_alerts(alerts_cache["data"], result)
            raise UpstreamError("Alert dependencies are unavailable or stale.", status_code=503, partial=result)
        completed = time.time()
        # A derived alert must never appear newer, or stay fresh longer, than its inputs.
        return LoadedData(result, min([completed + CACHE_DURATION_SECS] + [entry["expiry"] for entry in dependencies]),
                          min([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(completed))]
                              + [entry["created_at"] for entry in dependencies if entry["created_at"]]))
    return await cached(alerts_cache, load, response)
