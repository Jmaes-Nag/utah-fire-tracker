"""Per-feed cache entries with single-flight refresh, failure backoff and freshness headers."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException
from shapely.errors import GEOSException

from . import config
from .util import UpstreamError, utc_stamp

logger = logging.getLogger("wildfire-tracker")


def new_cache():
    return {"data": None, "expiry": 0.0, "created_at": ""}


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


async def cached(entry, loader, response=None, ttl=config.CACHE_DURATION_SECS):
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
                entry["failure"] = (
                    exc
                    if isinstance(exc, UpstreamError)
                    else UpstreamError(
                        "Unable to connect to upstream service."
                        if isinstance(exc, httpx.RequestError)
                        else "Upstream service returned invalid data.",
                        status_code=503 if isinstance(exc, httpx.RequestError) else 502,
                    )
                )
                entry["retry_after"] = time.time() + config.RETRY_BACKOFF_SECS
                entry["failure_created_at"] = utc_stamp()
            else:
                completed = time.time()
                expiry = completed + ttl
                created_at = utc_stamp(completed)
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
