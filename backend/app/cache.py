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


@dataclass
class Snapshot:
    """What a cache entry serves: the payload plus the freshness the client must be told."""

    data: Any
    created_at: str
    stale: bool = False

    def send(self, response):
        """Write the freshness headers onto an HTTP response and return the payload."""
        if response is not None:
            response.headers["X-Data-Timestamp"] = self.created_at
            if self.stale:
                response.headers["X-Data-Stale"] = "true"
            elif "X-Data-Stale" in response.headers:
                del response.headers["X-Data-Stale"]
        return self.data


async def cached(entry, loader, response=None, ttl=config.CACHE_DURATION_SECS):
    """Serve an entry's payload and write its freshness headers onto the response."""
    return (await refresh(entry, loader, ttl)).send(response)


async def refresh(entry, loader, ttl=config.CACHE_DURATION_SECS) -> Snapshot:
    """Coalesce refreshes, including failed refreshes; never renew stale data's age."""
    lock = entry.setdefault("lock", asyncio.Lock())
    async with lock:
        now = time.time()
        if entry["data"] is not None and now < entry["expiry"]:
            return Snapshot(entry["data"], entry.get("created_at", ""), stale=entry.get("incomplete", False))
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
                return Snapshot(data, created_at, stale=incomplete or completed >= expiry)
        if entry["data"] is not None:
            failure = entry.get("failure")
            data = failure.partial if failure and failure.partial is not None else entry["data"]
            return Snapshot(data, entry.get("created_at", ""), stale=True)
        failure = entry["failure"]
        if failure.partial is not None:
            # Cold-start degraded alerts are visible but never cached as a fresh all-clear.
            return Snapshot(failure.partial, entry["failure_created_at"], stale=True)
        raise HTTPException(status_code=failure.status_code, detail=str(failure))
