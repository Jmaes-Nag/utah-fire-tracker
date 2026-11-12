"""Settings, upstream service URLs and tuning constants."""

import os
from pathlib import Path

ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",") if origin.strip()]
NWS_USER_AGENT = os.getenv("NWS_USER_AGENT", "UtahWildfireTracker/1.0 (contact@example.com)")
NIFC_API_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query"
PERIMETERS_URL = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/ArcGIS/rest/services/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
HOTSPOTS_URL = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Fire_Detections_(v1)/FeatureServer/0/query"
SMOKE_URL = "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0/query"
SMOKE_FALLBACK_URL = "https://services2.arcgis.com/r6iFVcMJeA4kB4GC/arcgis/rest/services/NOAA_HMS_Smoke_Detection_Replica/FeatureServer/0/query"
AQI_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/Air_Quality_PM25_Latest_Results/FeatureServer/0/query"
CAP_URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/ArcGIS/rest/services/CAP_Alerts_Feed/FeatureServer/0/query"
BOUNDARIES_URL = (
    "https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services/UtahMunicipalBoundaries/FeatureServer/0/query"
)
BOUNDARIES_PATH = Path(__file__).resolve().parent / "data" / "utah_municipal_boundaries.geojson"
ALLOWED_NWS_HOSTS = {"api.weather.gov"}
# West, south, east, north: an approximate envelope, not the exact state boundary.
UTAH_BOUNDS = (-114.05, 37.0, -109.05, 42.0)
CACHE_DURATION_SECS = 15 * 60
RETRY_BACKOFF_SECS = 30
BOUNDARY_MAX_BACKOFF_SECS = CACHE_DURATION_SECS
BOUNDARY_MAX_BACKGROUND_ATTEMPTS = 5
WEATHER_CACHE_LIMIT = 1000
MAX_OBSERVATION_AGE_SECS = 2 * 60 * 60
