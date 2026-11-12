"""Utah municipal boundaries: validation, atomic persistence and recovery with backoff."""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from . import arcgis, config
from .geometry import UTAH_ENVELOPE, polygon_geometry
from .util import UpstreamError, mapping

logger = logging.getLogger("wildfire-tracker")


def new_boundary_state():
    return {"retry_after": 0.0, "failures": 0}


muni_boundaries: list[dict[str, Any]] = []
boundary_state = new_boundary_state()


def parse_boundaries(data):
    features = arcgis.feature_list(data, geojson=True)
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
            result.append(
                {
                    "name": props.get("NAME") or "Unknown",
                    "object_id": props.get("OBJECTID"),
                    "fips": props.get("FIPS"),
                    "geometry": geom,
                    "centroid": (geom.centroid.y, geom.centroid.x),
                }
            )
        except (ValueError, TypeError, KeyError):
            malformed = True
    if malformed:
        raise ValueError("Incomplete municipal boundary snapshot")
    if not result:
        raise ValueError("No valid Utah municipal boundaries")
    return result


def persist_boundaries(data):
    """Persist validated boundaries atomically; never leave a truncated cache file."""
    config.BOUNDARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config.BOUNDARIES_PATH.parent, delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config.BOUNDARIES_PATH)
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
            data = await arcgis.fetch_arcgis(
                config.BOUNDARIES_URL,
                {"where": "1=1", "outFields": "NAME,OBJECTID,FIPS", "outSR": "4326", "f": "geojson"},
                timeout=60,
            )
            loaded = parse_boundaries(data)
        except (httpx.RequestError, UpstreamError, ValueError):
            failures = boundary_state.get("failures", 0) + 1
            backoff = min(config.RETRY_BACKOFF_SECS * (2 ** (failures - 1)), config.BOUNDARY_MAX_BACKOFF_SECS)
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


async def boundary_recovery_loop(max_attempts: int = config.BOUNDARY_MAX_BACKGROUND_ATTEMPTS) -> bool:
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


async def load_boundaries():
    global muni_boundaries
    try:
        with config.BOUNDARIES_PATH.open(encoding="utf-8") as handle:
            loaded = parse_boundaries(json.load(handle))
    except (OSError, ValueError, UpstreamError):
        logger.warning("Municipal boundary file missing/invalid; attempting SGID recovery.")
        await recover_boundaries(force=True)
        return
    muni_boundaries = loaded
    boundary_state.update(failures=0, retry_after=0.0)
    logger.info("Loaded %d Utah municipal boundaries.", len(loaded))
