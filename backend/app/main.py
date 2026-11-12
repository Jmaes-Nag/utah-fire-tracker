"""FastAPI application: middleware, startup and route registration."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import alerts, boundaries, config, feeds, weather

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app):
    await boundaries.load_boundaries()
    task = asyncio.create_task(boundaries.boundary_recovery_loop()) if not boundaries.muni_boundaries else None
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=config.ALLOWED_ORIGINS != ["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["X-Data-Timestamp", "X-Data-Stale"],
)


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "time": time.time()}


for module in (feeds, weather, alerts):
    app.include_router(module.router)
