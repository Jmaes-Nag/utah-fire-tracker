"""ArcGIS feature-service client: validation, complete pagination and query parameters."""

import json

import httpx

from . import config
from .util import UpstreamError, mapping


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


def spatial_params(fields, where="1=1"):
    return {
        "where": where,
        "geometryType": "esriGeometryEnvelope",
        "geometry": ",".join(str(value) for value in config.UTAH_BOUNDS),
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outSR": "4326",
        "outFields": fields,
        "f": "json",
    }
