"""FastAPI application.

Routers are added as the build order progresses: health, sim, the agent tools and the inbound
SMS webhook are up; the dashboard WebSocket lands in step 5.

The refill worker runs inside this process (charter D3: one process until it hurts). It is the
only background loop in the app, and it is started here rather than inside the refill package
so that importing `app.refill.engine` from a CLI or a test never starts a timer.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import get_sessionmaker
from app.refill.worker import RefillWorker
from app.routers import agent, calls, dash, health, sim, sms

logger = logging.getLogger("vakans")


def _configure_logging() -> None:
    """Make our own loggers visible under uvicorn.

    Uvicorn installs its own handlers and leaves the root logger at WARNING, so every
    `logger.info` in this codebase is silently dropped by default. That matters here more than
    it usually would: `tail -f` on the API log is how you watch a refill cascade advance and how
    you read the fake SMS bodies when no phone is in the room.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s  %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    settings = get_settings()
    # safe_database_url, never database_url — the latter carries the password.
    logger.info("starting vakans env=%s db=%s", settings.env, settings.safe_database_url)

    worker: RefillWorker | None = None
    if settings.refill_worker_enabled:
        worker = RefillWorker(get_sessionmaker(), settings)
        worker.start()
    else:
        logger.warning("refill worker disabled — cascades will not advance on their own")

    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Vakans sandbox API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Scoped to the dashboard's own origins, never "*": /sim/* is destructive and a browser
    # on any page could otherwise reset a demo that is mid-meeting.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(agent.router)
    app.include_router(sim.router)
    app.include_router(dash.router)
    app.include_router(sms.router)
    app.include_router(calls.router)
    return app


app = create_app()
