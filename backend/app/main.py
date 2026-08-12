"""FastAPI application.

Routers are added as the build order progresses: health + sim now (step 1), agent tools and
refill webhooks in step 3, the dashboard WebSocket in step 5.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.routers import health, sim

logger = logging.getLogger("vakans")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # safe_database_url, never database_url — the latter carries the password.
    logger.info("starting vakans env=%s db=%s", settings.env, settings.safe_database_url)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vakans sandbox API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(sim.router)
    return app


app = create_app()
