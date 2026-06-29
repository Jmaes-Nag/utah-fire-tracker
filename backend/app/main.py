import os
import time
import logging
import math
import json
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
from shapely.geometry import Point, Polygon, shape
from shapely.affinity import translate

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wildfire-tracker")

app = FastAPI(title="Utah Wildfire and Wind Tracking API")

# Global variables
muni_boundaries: List[Dict[str, any]] = []

# Configure CORS dynamically based on environment configuration
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*")
if allowed_origins_env == "*":
    origins = ["*"]
    allow_creds = False  # Wildcard origins cannot combine with allow_credentials=True in modern browsers
else:
    origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()]
    allow_creds = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_creds,
    allow_methods=["GET"],  # Restrict to GET requests as this is a read-only dashboard
    allow_headers=["*"],
)

# Configuration & Cache Setup
NWS_USER_AGENT = os.getenv("NWS_USER_AGENT", "UtahWildfireTracker/1.0 (contact@example.com)")
NIFC_API_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"

# 15-minute in-memory cache
cache: Dict[str, any] = {
    "data": None,
    "expiry": 0.0,
    "created_at": ""
}
perimeter_cache: Dict[str, any] = {
    "data": None,
    "expiry": 0.0,
    "created_at": ""
}
hotspots_cache: Dict[str, any] = {
    "data": None,
    "expiry": 0.0,
    "created_at": ""
}
weather_cache: Dict[str, any] = {}
CACHE_DURATION_SECS = 15 * 60

def get_cardinal_direction(degrees: Optional[float]) -> str:
    """Helper to convert wind degrees to a compass cardinal direction."""
    if degrees is None:
        return "N/A"
    degrees = degrees % 360
    directions = [
        "N", "NNE", "NE", "ENE",
        "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW",
        "W", "WNW", "NW", "NNW"
    ]
    idx = int((degrees + 11.25) / 22.5) % 16
    return directions[idx]

@app.get("/api/health")
async def health_check():
    """Service health check endpoint."""
    return {"status": "ok", "time": time.time()}

@app.get("/api/incidents")
async def get_incidents(response: Response = None):
    """
    Fetch active wildfire incidents in Utah from NIFC.
    Uses an in-memory cache to prevent rate-limiting public services.
    """
    now = time.time()
    
    # Return cached data if still valid
    if cache["data"] is not None and now < cache["expiry"]:
        logger.info("Serving incident data from in-memory cache.")
        if response:
            response.headers["X-Data-Timestamp"] = cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
        return cache["data"]
        
    logger.info("Cache expired or empty. Fetching from NIFC ArcGIS REST API...")
    
    params = {
        "where": "POOState='US-UT' OR POOState='UT'",
        "outFields": "*",
        "outSR": "4326",  # WGS84 coordinates
        "f": "json"       # Return JSON format
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(NIFC_API_URL, params=params)
            
        if response.status_code != 200:
            logger.error(f"NIFC API returned HTTP status {response.status_code}")
            raise HTTPException(status_code=502, detail="Bad Gateway: NIFC API returned error status.")
            
        raw_data = response.json()
        features = raw_data.get("features", [])
        
        cleaned_incidents = []
        for feature in features:
            attributes = feature.get("attributes", {})
            geometry = feature.get("geometry", {})
            
            # Extract coordinates (WGS84: x=longitude, y=latitude)
            lon = geometry.get("x")
            lat = geometry.get("y")
            
            # Skip records without valid coordinates
            if lat is None or lon is None:
                continue
                
            cleaned_incidents.append({
                "id": attributes.get("UniqueFireIdentifier") or str(attributes.get("OBJECTID")),
                "object_id": attributes.get("OBJECTID"),
                "name": attributes.get("IncidentName", "Unknown Wildfire"),
                "acres": attributes.get("IncidentSize") or attributes.get("DailyAcres") or attributes.get("CalculatedAcres") or 0.0,
                "containment": attributes.get("PercentContained"),
                "discovered": attributes.get("FireDiscoveryDateTime") or attributes.get("IncidentStartDate"),
                "cause": attributes.get("FireCause") or attributes.get("FireCauseGeneral") or "Undetermined",
                "county": attributes.get("POOCounty", "Unknown County"),
                "latitude": lat,
                "longitude": lon
            })
            
        # Store clean incidents in cache
        cache["data"] = cleaned_incidents
        cache["expiry"] = now + CACHE_DURATION_SECS
        cache["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        logger.info(f"Successfully cached {len(cleaned_incidents)} active Utah wildfires.")
        
        if response:
            response.headers["X-Data-Timestamp"] = cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
        return cleaned_incidents
        
    except httpx.RequestError as e:
        logger.exception("HTTP Request to NIFC API failed.")
        # Fallback to stale cache if NIFC is down
        if cache["data"] is not None:
            logger.warning("Serving stale cached incident data due to external API outage.")
            return cache["data"]
        raise HTTPException(status_code=503, detail=f"Service Unavailable: Failed to connect to NIFC API: {str(e)}")
    except Exception as e:
        logger.exception("Unexpected error fetching incidents.")
        if cache["data"] is not None:
            logger.warning("Serving stale cached incident data due to unexpected error.")
            return cache["data"]
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

@app.get("/api/perimeters")
async def get_perimeters(response: Response = None):
    """
    Fetch active wildfire burn perimeters in Utah from NIFC WFIGS.
    Uses an in-memory cache to prevent rate-limiting public services.
    """
    now = time.time()
    
    # Return cached data if still valid
    if perimeter_cache["data"] is not None and now < perimeter_cache["expiry"]:
        logger.info("Serving perimeter data from in-memory cache.")
        if response:
            response.headers["X-Data-Timestamp"] = perimeter_cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
        return perimeter_cache["data"]
        
    logger.info("Perimeters cache expired or empty. Fetching from NIFC WFIGS Interagency Perimeters REST API...")
    
    url = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
    params = {
        "where": "attr_POOState='US-UT' OR attr_POOState='UT'",
        "outSR": "4326",
        "f": "geojson"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=params)
            
        if res.status_code != 200:
            logger.error(f"NIFC Perimeters API returned HTTP status {res.status_code}")
            raise HTTPException(status_code=502, detail="Bad Gateway: NIFC Perimeters API returned error status.")
            
        raw_data = res.json()
        
        # Store raw GeoJSON FeatureCollection in cache
        perimeter_cache["data"] = raw_data
        perimeter_cache["expiry"] = now + CACHE_DURATION_SECS
        perimeter_cache["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        logger.info(f"Successfully cached perimeters from NIFC WFIGS.")
        
        if response:
            response.headers["X-Data-Timestamp"] = perimeter_cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
        return raw_data
        
    except httpx.RequestError as e:
        logger.exception("HTTP Request to NIFC Perimeters API failed.")
        if perimeter_cache["data"] is not None:
            logger.warning("Serving stale cached perimeter data due to external API outage.")
            return perimeter_cache["data"]
        raise HTTPException(status_code=503, detail=f"Service Unavailable: Failed to connect to NIFC Perimeters API: {str(e)}")
    except Exception as e:
        logger.exception("Unexpected error fetching perimeters.")
        if perimeter_cache["data"] is not None:
            logger.warning("Serving stale cached perimeter data due to unexpected error.")
            return perimeter_cache["data"]
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

@app.get("/api/hotspots")
async def get_hotspots(response: Response = None):
    """
    Fetch active satellite-detected thermal hotspots (MODIS/VIIRS) from NOAA HMS.
    Filters by Utah bounding box envelope.
    """
    now = time.time()
    if hotspots_cache["data"] is not None and now < hotspots_cache["expiry"]:
        logger.info("Serving thermal hotspots data from cache.")
        if response:
            response.headers["X-Data-Timestamp"] = hotspots_cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
        return hotspots_cache["data"]
        
    logger.info("Hotspots cache expired or empty. Fetching from NOAA HMS REST API...")
    url = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Fire_Detections_(v1)/FeatureServer/0/query"
    params = {
        "where": "1=1",
        "geometryType": "esriGeometryEnvelope",
        "geometry": "-114.05,37.0,-109.05,42.0",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "FID,Lon,Lat,YearDay,Time,Satellite,Method,FRP",
        "f": "json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.get(url, params=params)
            
        if res.status_code != 200:
            logger.error(f"NOAA Hotspots API returned HTTP status {res.status_code}")
            raise HTTPException(status_code=502, detail="Failed to query NOAA Hotspots service.")
            
        raw_data = res.json()
        features = raw_data.get("features", [])
        
        cleaned = []
        for feat in features:
            attribs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            
            lon = geom.get("x") or attribs.get("Lon")
            lat = geom.get("y") or attribs.get("Lat")
            
            if lat is None or lon is None:
                continue
                
            cleaned.append({
                "fid": attribs.get("FID"),
                "latitude": lat,
                "longitude": lon,
                "satellite": attribs.get("Satellite", "Unknown"),
                "method": attribs.get("Method", "Unknown"),
                "frp": attribs.get("FRP", 0.0),
                "time": attribs.get("Time", ""),
                "yearday": attribs.get("YearDay", "")
            })
            
        hotspots_cache["data"] = cleaned
        hotspots_cache["expiry"] = now + CACHE_DURATION_SECS
        hotspots_cache["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        logger.info(f"Successfully cached {len(cleaned)} hotspots.")
        
        if response:
            response.headers["X-Data-Timestamp"] = hotspots_cache["created_at"]
            response.headers["Access-Control-Expose-Headers"] = "X-Data-Timestamp"
            
        return cleaned
    except Exception as e:
        logger.exception("Error fetching hotspots.")
        if hotspots_cache["data"] is not None:
            logger.warning("Serving stale cached hotspots.")
            return hotspots_cache["data"]
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/weather")
async def get_weather(
    latitude: float = Query(..., description="Latitude coordinate"),
    longitude: float = Query(..., description="Longitude coordinate")
):
    """
    On-demand weather telemetry retrieval from api.weather.gov for a given lat/lon.
    Queries up to 3 nearby NWS stations to handle outages and null observation values.
    """
    # Evict cache entries if memory footprint grows
    if len(weather_cache) > 1000:
        weather_cache.clear()
        logger.info("Evicted weather cache to manage memory footprint.")

    cache_key = f"{latitude:.3f},{longitude:.3f}"
    now = time.time()
    if cache_key in weather_cache:
        cached_item = weather_cache[cache_key]
        if now < cached_item["expiry"]:
            logger.info(f"Serving weather data for {cache_key} from in-memory cache.")
            return cached_item["data"]

    headers = {"User-Agent": NWS_USER_AGENT}
    points_url = f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}"
    
    async with httpx.AsyncClient(headers=headers, timeout=10.0, follow_redirects=True) as client:
        try:
            # 1. Retrieve the point grid information to get the stations endpoint
            logger.info(f"Querying NWS points endpoint: {points_url}")
            points_res = await client.get(points_url)
            
            if points_res.status_code != 200:
                logger.error(f"NWS points endpoint returned {points_res.status_code}: {points_res.text}")
                raise HTTPException(status_code=502, detail="Failed to locate weather grid points from api.weather.gov.")
                
            points_data = points_res.json()
            stations_url = points_data.get("properties", {}).get("observationStations")
            
            if not stations_url:
                raise HTTPException(status_code=502, detail="No weather observation stations link returned by api.weather.gov.")
                
            # 2. Get list of nearby stations
            logger.info(f"Querying NWS stations list: {stations_url}")
            stations_res = await client.get(stations_url)
            if stations_res.status_code != 200:
                raise HTTPException(status_code=502, detail="Failed to retrieve observation stations list.")
                
            stations_data = stations_res.json()
            features = stations_data.get("features", [])
            
            if not features:
                raise HTTPException(status_code=404, detail="No nearby weather observation stations found.")
                
            # Extract up to 3 stations
            stations_to_try = features[:3]
            last_error_detail = "Failed to query observations"
            
            # 3. Query stations sequentially (fallback up to 3 stations if observation data is missing or errors out)
            for idx, station_feature in enumerate(stations_to_try):
                station_props = station_feature.get("properties", {})
                station_id = station_props.get("stationIdentifier")
                station_name = station_props.get("name", "Unknown Station")
                
                if not station_id:
                    continue
                    
                obs_url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
                logger.info(f"Attempting station ({idx+1}/{len(stations_to_try)}): {station_id} at {obs_url}")
                
                try:
                    obs_res = await client.get(obs_url)
                    if obs_res.status_code != 200:
                        logger.warning(f"Station {station_id} returned status code {obs_res.status_code}")
                        continue
                        
                    obs_data = obs_res.json()
                    properties = obs_data.get("properties", {})
                    
                    # Extract telemetry properties
                    wind_speed_obj = properties.get("windSpeed") or {}
                    wind_dir_obj = properties.get("windDirection") or {}
                    temp_obj = properties.get("temperature") or {}
                    rh_obj = properties.get("relativeHumidity") or {}
                    
                    wind_speed_ms = wind_speed_obj.get("value")
                    wind_dir_deg = wind_dir_obj.get("value")
                    temp_c = temp_obj.get("value")
                    relative_humidity = rh_obj.get("value")
                    
                    # If wind data is missing, treat it as a failure and try the next station
                    if wind_speed_ms is None or wind_dir_deg is None:
                        logger.warning(f"Station {station_id} observations had missing/null wind telemetry. Trying next station.")
                        last_error_detail = f"Station {station_id} had null wind measurements."
                        continue
                    
                    # Unit conversions:
                    # NWS speed is returned in km/h or m/s. 
                    # Checking unitCode to perform correct conversion, standard unit is usually m/s or km/h.
                    # NWS API docs define standard unit for windSpeed as wmoUnit:km_h-1 (which is km/h).
                    # If it represents km/h, speed_mph = km/h * 0.621371.
                    # If it is m/s, speed_mph = m/s * 2.23694. Let's inspect unitCode.
                    unit_code = wind_speed_obj.get("unitCode", "")
                    
                    raw_speed = float(wind_speed_ms)
                    if "km_h-1" in unit_code:
                        wind_speed_kmh = raw_speed
                        wind_speed_mph = raw_speed * 0.621371
                    elif "m_s-1" in unit_code:
                        wind_speed_kmh = raw_speed * 3.6
                        wind_speed_mph = raw_speed * 2.23694
                    else:
                        # Fallback default assuming km/h
                        wind_speed_kmh = raw_speed
                        wind_speed_mph = raw_speed * 0.621371
                        
                    # Temperature conversions (C to F)
                    temp_f = None
                    if temp_c is not None:
                        temp_f = float(temp_c) * 1.8 + 32
                        
                    cardinal = get_cardinal_direction(wind_dir_deg)
                    
                    logger.info(f"Successfully retrieved telemetry from station {station_id}")
                    weather_data = {
                        "station_id": station_id,
                        "station_name": station_name,
                        "wind_speed_mph": round(wind_speed_mph, 1),
                        "wind_speed_kmh": round(wind_speed_kmh, 1),
                        "wind_direction_deg": round(wind_dir_deg, 1),
                        "wind_direction_cardinal": cardinal,
                        "temperature_f": round(temp_f, 1) if temp_f is not None else None,
                        "temperature_c": round(temp_c, 1) if temp_c is not None else None,
                        "relative_humidity": round(relative_humidity, 1) if relative_humidity is not None else None,
                        "timestamp": properties.get("timestamp")
                    }
                    weather_cache[cache_key] = {
                        "data": weather_data,
                        "expiry": now + 300  # cache for 5 minutes
                    }
                    return weather_data
                    
                except Exception as ex:
                    logger.warning(f"Error querying station {station_id}: {str(ex)}")
                    last_error_detail = str(ex)
                    continue
            
            # If we fall through the loop, we could not get valid wind data
            raise HTTPException(status_code=502, detail=f"Failed to fetch valid meteorological observations from any nearby station. Last error: {last_error_detail}")
            
        except httpx.RequestError as e:
            logger.exception("HTTP connection error to NWS api.weather.gov")
            raise HTTPException(status_code=503, detail=f"Service Unavailable: Failed to connect to Weather Service: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """
    On startup, download the Utah municipal boundaries GeoJSON if not present,
    and parse all municipal boundary geometries into memory as Shapely polygons.
    """
    os.makedirs("app/data", exist_ok=True)
    geojson_path = "app/data/utah_municipal_boundaries.geojson"
    
    if not os.path.exists(geojson_path):
        logger.info("Utah Municipal Boundaries GeoJSON not found. Downloading from SGID...")
        url = "https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/UtahMunicipalBoundaries/FeatureServer/0/query?where=1%3D1&outFields=NAME,OBJECTID,FIPS&outSR=4326&f=geojson"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    with open(geojson_path, "w") as f:
                        f.write(res.text)
                    logger.info("Successfully downloaded and saved municipal boundaries GeoJSON.")
                else:
                    logger.error(f"Failed to download boundaries: HTTP {res.status_code}")
        except Exception as e:
            logger.exception("Error downloading municipal boundaries.")
            
    if os.path.exists(geojson_path):
        try:
            with open(geojson_path, "r") as f:
                muni_data = json.load(f)
            
            global muni_boundaries
            muni_boundaries = []
            for feature in muni_data.get("features", []):
                geom = shape(feature["geometry"])
                props = feature.get("properties", {})
                muni_boundaries.append({
                    "name": props.get("NAME", "Unknown"),
                    "object_id": props.get("OBJECTID"),
                    "fips": props.get("FIPS"),
                    "geometry": geom,
                    "centroid": (geom.centroid.y, geom.centroid.x) # (lat, lon)
                })
            logger.info(f"Loaded {len(muni_boundaries)} Utah municipal boundaries.")
        except Exception as e:
            logger.exception("Error loading municipal boundaries GeoJSON.")

@app.get("/api/alerts")
async def get_alerts():
    """
    Predictive 'Cities At Risk' alerting engine.
    Applies wind vectors to active Utah wildfires, projects downwind threat zones,
    and intersects them with Utah municipal boundary polygons.
    Also queries active FEMA CAP Alerts and overlays them onto the risk queue.
    """
    # 1. Fetch active fires (making use of cache)
    try:
        incidents = await get_incidents()
    except Exception as e:
        logger.error(f"Failed to fetch incidents for threat check: {str(e)}")
        incidents = []
        
    alerts = {} # maps city_name -> alert dict
    
    # 2. Query active FEMA CAP Alerts
    fema_alerts = []
    cap_url = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/CAP_Alerts_Feed/FeatureServer/0/query"
    cap_params = {
        "where": "countryCode='us' AND (event LIKE '%Wildfire%' OR event LIKE '%Evacuate%' OR event LIKE '%Evacuation%')",
        "outFields": "*",
        "outSR": "4326",
        "f": "json"
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            cap_res = await client.get(cap_url, params=cap_params)
        if cap_res.status_code == 200:
            cap_data = cap_res.json()
            fema_alerts = cap_data.get("features", [])
            logger.info(f"Fetched {len(fema_alerts)} active wildfire/evacuation alerts globally.")
    except Exception as e:
        logger.warning(f"Failed to query CAP Alerts Feed: {str(e)}")

    # Parse active FEMA alert polygons into memory
    parsed_fema = []
    for alert in fema_alerts:
        attribs = alert.get("attributes", {})
        geometry = alert.get("geometry", {})
        if not geometry or "rings" not in geometry:
            continue
        try:
            rings = geometry["rings"]
            if len(rings) == 1:
                poly = Polygon(rings[0])
            else:
                poly = Polygon(rings[0], rings[1:])
            parsed_fema.append({
                "attributes": attribs,
                "geometry": poly,
                "rings": rings
            })
        except Exception as e:
            logger.warning(f"Error parsing alert polygon: {str(e)}")

    # 3. Perform spatial overlay for each incident
    DEG_PER_MILE = 0.0145
    BASE_BUFFER_DEG = 5.0 * DEG_PER_MILE  # 5 miles base safety buffer
    
    for incident in incidents:
        fire_lat = incident.get("latitude")
        fire_lon = incident.get("longitude")
        fire_name = incident.get("name")
        fire_id = incident.get("id")
        
        if fire_lat is None or fire_lon is None:
            continue
            
        # Get wind telemetry for this fire coordinates
        wind_speed_mph = 0.0
        wind_dir_deg = None
        wind_cardinal = "N/A"
        try:
            weather = await get_weather(latitude=fire_lat, longitude=fire_lon)
            wind_speed_mph = weather.get("wind_speed_mph", 0.0)
            wind_dir_deg = weather.get("wind_direction_deg")
            wind_cardinal = weather.get("wind_direction_cardinal", "N/A")
        except Exception as e:
            logger.warning(f"Could not retrieve wind for {fire_name}: {str(e)}")
            
        fire_point = Point(fire_lon, fire_lat)
        
        # Calculate wind directional shift (downwind heading = wind heading + 180)
        dx = 0.0
        dy = 0.0
        if wind_dir_deg is not None and wind_speed_mph > 0:
            downwind_deg = (wind_dir_deg + 180) % 360
            trig_deg = 90 - downwind_deg
            trig_rad = math.radians(trig_deg)
            # Shift length: wind_speed * 0.2 miles
            shift_miles = wind_speed_mph * 0.2
            shift_deg = shift_miles * DEG_PER_MILE
            dx = math.cos(trig_rad) * shift_deg
            dy = math.sin(trig_rad) * shift_deg
            
        # Compute threat zone (union of point and translated downwind point, buffered)
        shifted_fire_point = translate(fire_point, xoff=dx, yoff=dy)
        threat_zone = fire_point.union(shifted_fire_point).buffer(BASE_BUFFER_DEG)
        
        # Cross-reference with all loaded municipal boundaries
        for muni in muni_boundaries:
            muni_geom = muni["geometry"]
            muni_name = muni["name"]
            
            if threat_zone.intersects(muni_geom):
                dist_deg = fire_point.distance(muni_geom)
                dist_miles = dist_deg / DEG_PER_MILE
                
                # Check if it intersects the base buffer directly (Level 2: Set) vs. just the wind plume (Level 1: Ready)
                base_zone = fire_point.buffer(BASE_BUFFER_DEG)
                is_direct = base_zone.intersects(muni_geom)
                
                risk_level = 2 if (dist_miles <= 5.0 or is_direct) else 1
                
                # Add or update municipality record in alerts queue
                if muni_name in alerts:
                    if risk_level > alerts[muni_name]["risk_level"]:
                        alerts[muni_name]["risk_level"] = risk_level
                        alerts[muni_name]["risk_level_text"] = "Set" if risk_level == 2 else "Ready"
                    
                    # Prevent duplicate fire sources
                    if not any(f["id"] == fire_id for f in alerts[muni_name]["fires"]):
                        alerts[muni_name]["fires"].append({
                            "name": fire_name,
                            "id": fire_id,
                            "distance_miles": round(dist_miles, 1),
                            "wind_speed_mph": wind_speed_mph,
                            "wind_direction": wind_cardinal,
                            "wind_direction_deg": wind_dir_deg
                        })
                else:
                    alerts[muni_name] = {
                        "city": muni_name,
                        "object_id": muni["object_id"],
                        "fips": muni["fips"],
                        "risk_level": risk_level,
                        "risk_level_text": "Set" if risk_level == 2 else "Ready",
                        "centroid": muni["centroid"],
                        "fires": [{
                            "name": fire_name,
                            "id": fire_id,
                            "distance_miles": round(dist_miles, 1),
                            "wind_speed_mph": wind_speed_mph,
                            "wind_direction": wind_cardinal,
                            "wind_direction_deg": wind_dir_deg
                        }],
                        "fema_alerts": []
                    }

    # 4. Cross-reference municipal boundaries against active FEMA CAP warnings
    for muni in muni_boundaries:
        muni_geom = muni["geometry"]
        muni_name = muni["name"]
        
        for p_fema in parsed_fema:
            fema_geom = p_fema["geometry"]
            fema_attr = p_fema["attributes"]
            
            if fema_geom.intersects(muni_geom):
                if muni_name not in alerts:
                    alerts[muni_name] = {
                        "city": muni_name,
                        "object_id": muni["object_id"],
                        "fips": muni["fips"],
                        "risk_level": 2, # Upgrade to Set immediately
                        "risk_level_text": "Set",
                        "centroid": muni["centroid"],
                        "fires": [],
                        "fema_alerts": []
                    }
                else:
                    alerts[muni_name]["risk_level"] = 2
                    alerts[muni_name]["risk_level_text"] = "Set"
                    
                alerts[muni_name]["fema_alerts"].append({
                    "event": fema_attr.get("event"),
                    "headline": fema_attr.get("headline"),
                    "description": fema_attr.get("description"),
                    "instruction": fema_attr.get("instruction"),
                    "severity": fema_attr.get("severity"),
                    "urgency": fema_attr.get("urgency"),
                    "expires": fema_attr.get("expires"),
                    "rings": p_fema["rings"]
                })

    # Sort results: Level 2 (Set) first, then by closest fire distance
    alerts_list = list(alerts.values())
    
    def get_sort_key(item):
        level = item["risk_level"]
        distances = [f["distance_miles"] for f in item["fires"]]
        min_dist = min(distances) if distances else 999.0
        return (-level, min_dist)
        
    alerts_list.sort(key=get_sort_key)
    return alerts_list

# Caches for smoke and air quality
smoke_cache: Dict[str, any] = {
    "data": None,
    "expiry": 0.0
}
aqi_cache: Dict[str, any] = {
    "data": None,
    "expiry": 0.0
}

def calculate_pm25_aqi(pm25: float) -> int:
    """
    Calculate the US EPA Air Quality Index (AQI) for PM2.5 concentration.
    Follows official EPA breakpoints.
    """
    if pm25 is None or pm25 < 0:
        return 0
    # Round PM2.5 to one decimal place as specified by EPA
    pm25 = round(pm25, 1)
    
    # Official EPA Breakpoints (C_low, C_high, I_low, I_high)
    if pm25 <= 9.0:
        return int(round(((50 - 0) / (9.0 - 0.0)) * (pm25 - 0.0) + 0))
    elif pm25 <= 35.4:
        return int(round(((100 - 51) / (35.4 - 9.1)) * (pm25 - 9.1) + 51))
    elif pm25 <= 55.4:
        return int(round(((150 - 101) / (55.4 - 35.5)) * (pm25 - 35.5) + 101))
    elif pm25 <= 125.4:
        return int(round(((200 - 151) / (125.4 - 55.5)) * (pm25 - 55.5) + 151))
    elif pm25 <= 225.4:
        return int(round(((300 - 201) / (225.4 - 125.5)) * (pm25 - 125.5) + 201))
    elif pm25 <= 325.4:
        return int(round(((400 - 301) / (325.4 - 225.5)) * (pm25 - 225.5) + 301))
    else:
        # Cap index at 500
        val = ((500 - 401) / (500.0 - 325.5)) * (min(pm25, 500.0) - 325.5) + 401
        return int(round(min(val, 500.0)))

@app.get("/api/smoke")
async def get_smoke():
    """
    Fetch active smoke plume polygons from NOAA HMS Smoke Detection layer.
    Filters by Utah bounding box envelope.
    """
    now = time.time()
    if smoke_cache["data"] is not None and now < smoke_cache["expiry"]:
        logger.info("Serving smoke plume data from cache.")
        return smoke_cache["data"]
        
    primary_url = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0/query"
    fallback_url = "https://services2.arcgis.com/r6iFVcMJeA4kB4GC/arcgis/rest/services/NOAA_HMS_Smoke_Detection_Replica/FeatureServer/0/query"
    
    params = {
        "where": "1=1",
        "geometryType": "esriGeometryEnvelope",
        "geometry": "-114.05,37.0,-109.05,42.0",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "FID,Satellite,Start,End_,Density",
        "f": "json"
    }
    
    features = []
    source_used = "primary"
    
    try:
        # Try primary URL first
        logger.info("Fetching smoke plumes from primary NOAA service...")
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(primary_url, params=params)
            
        if res.status_code == 200:
            features = res.json().get("features", [])
            
        # If primary failed or returned 0 features, try fallback replica
        if res.status_code != 200 or not features:
            logger.info("Primary NOAA service returned empty or error. Trying fallback replica service...")
            source_used = "fallback"
            async with httpx.AsyncClient(timeout=15.0) as client:
                res_fb = await client.get(fallback_url, params=params)
            if res_fb.status_code == 200:
                features = res_fb.json().get("features", [])
            else:
                logger.error(f"Fallback NOAA Smoke API returned status {res_fb.status_code}")
                if res.status_code != 200:
                    # If both failed, raise error
                    raise HTTPException(status_code=502, detail="Failed to query NOAA Smoke Plume service and fallback.")
        
        normalized = []
        for feat in features:
            attribs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            if not geom or "rings" not in geom:
                continue
                
            normalized.append({
                "fid": attribs.get("FID") or attribs.get("OBJECTID"),
                "satellite": attribs.get("Satellite"),
                "start": attribs.get("Start"),
                "end": attribs.get("End_"),
                "density": attribs.get("Density", "Light"),
                "rings": geom.get("rings")
            })
            
        smoke_cache["data"] = normalized
        smoke_cache["expiry"] = now + CACHE_DURATION_SECS
        logger.info(f"Successfully cached {len(normalized)} smoke plumes intersecting Utah using {source_used} source.")
        return normalized
    except Exception as e:
        logger.exception("Error fetching smoke plume data.")
        if smoke_cache["data"] is not None:
            logger.warning("Serving stale cached smoke data.")
            return smoke_cache["data"]
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/aqi")
async def get_aqi():
    """
    Fetch live PM2.5 measurements from government monitoring stations.
    Maps concentrations to EPA AQI values and health tiers.
    Filters by Utah bounding box envelope.
    """
    now = time.time()
    if aqi_cache["data"] is not None and now < aqi_cache["expiry"]:
        logger.info("Serving air quality data from cache.")
        return aqi_cache["data"]
        
    url = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/Air_Quality_PM25_Latest_Results/FeatureServer/0/query"
    params = {
        "where": "country='US'",
        "geometryType": "esriGeometryEnvelope",
        "geometry": "-114.05,37.0,-109.05,42.0",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "OBJECTID,location_id,city,location,lastUpdated,value,unit,provider_name,instrument_name",
        "f": "json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(url, params=params)
        if res.status_code != 200:
            logger.error(f"OpenAQ PM2.5 API returned HTTP status {res.status_code}")
            raise HTTPException(status_code=502, detail="Failed to query Air Quality service.")
            
        raw = res.json()
        features = raw.get("features", [])
        
        normalized = []
        for feat in features:
            attribs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            if not geom or "x" not in geom or "y" not in geom:
                continue
                
            pm25_val = attribs.get("value", 0.0)
            aqi = calculate_pm25_aqi(pm25_val)
            
            # Map AQI to EPA color and tier
            if aqi <= 50:
                color = "#10b981" # Green (Good)
                tier = "Good"
            elif aqi <= 100:
                color = "#eab308" # Yellow (Moderate)
                tier = "Moderate"
            elif aqi <= 150:
                color = "#f97316" # Orange (Unhealthy for Sensitive Groups)
                tier = "Unhealthy for Sensitive Groups"
            elif aqi <= 200:
                color = "#ef4444" # Red (Unhealthy)
                tier = "Unhealthy"
            elif aqi <= 300:
                color = "#a855f7" # Purple (Very Unhealthy)
                tier = "Very Unhealthy"
            else:
                color = "#7f1d1d" # Maroon (Hazardous)
                tier = "Hazardous"
                
            normalized.append({
                "id": attribs.get("location_id") or attribs.get("OBJECTID"),
                "city": attribs.get("city") or attribs.get("location", "Unknown Location"),
                "name": attribs.get("location", "Unknown Monitor"),
                "pm25": pm25_val,
                "aqi": aqi,
                "tier": tier,
                "color": color,
                "latitude": geom.get("y"),
                "longitude": geom.get("x"),
                "provider": attribs.get("provider_name", "Government Monitor"),
                "last_updated": attribs.get("lastUpdated")
            })
            
        aqi_cache["data"] = normalized
        aqi_cache["expiry"] = now + CACHE_DURATION_SECS
        logger.info(f"Successfully cached {len(normalized)} Utah AQI monitoring stations.")
        return normalized
    except Exception as e:
        logger.exception("Error fetching air quality data.")
        if aqi_cache["data"] is not None:
            logger.warning("Serving stale cached air quality data.")
            return aqi_cache["data"]
        raise HTTPException(status_code=500, detail=str(e))

