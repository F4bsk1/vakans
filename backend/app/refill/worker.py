"""The background tick that makes the cascade move on its own.

Two jobs, both idempotent and both safe to run twice: drain the SMS outbox, and advance any
cascade whose 90-second window has closed.

Deliberately a **poll**, not a per-cascade `asyncio.sleep(90)`. A sleeping task lives only in
one process's memory, so restarting the API mid-demo would strand a `held` slot forever with
nobody left to advance it. Everything the worker needs is in the database, so a restart resumes
exactly where it left off — and the whole thing is a plain function you can call with a pinned
`now` from a test.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.refill import engine
from app.sms import deliver_queued
from app.timeutils import now_utc

logger = logging.getLogger("vakans.refill.worker")


@dataclass(frozen=True)
class TickResult:
    sms_sent: int = 0
    sms_failed: int = 0
    offers_expired: int = 0


async def tick(session: AsyncSession, *, settings: Settings, now: datetime) -> TickResult:
    """One pass. Delivery first, so an offer written this instant goes out before it is aged."""
    delivery = await deliver_queued(session, settings=settings)
    advanced = await engine.sweep_expired(session, settings=settings, now=now)
    # The advance itself queues the next SMS; drain again so a timeout and its follow-up offer
    # land in the same tick rather than a poll interval apart.
    if advanced:
        follow_up = await deliver_queued(session, settings=settings)
        delivery = type(delivery)(
            sent=delivery.sent + follow_up.sent, failed=delivery.failed + follow_up.failed
        )
    return TickResult(
        sms_sent=delivery.sent, sms_failed=delivery.failed, offers_expired=len(advanced)
    )


class RefillWorker:
    """Owns the loop. Started from the FastAPI lifespan, stopped on shutdown."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        interval_s: float | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._settings = settings
        self._interval = interval_s or settings.refill_worker_interval_s
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> TickResult:
        async with self._sessionmaker() as session:
            try:
                result = await tick(session, settings=self._settings, now=now_utc())
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result

    async def _loop(self) -> None:
        logger.info("refill worker started (every %.1fs)", self._interval)
        while True:
            try:
                result = await self.run_once()
                if result != TickResult():
                    logger.info(
                        "refill tick: %d sms sent, %d failed, %d offers expired",
                        result.sms_sent,
                        result.sms_failed,
                        result.offers_expired,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick must never kill the loop: a demo where the cascade silently
                # stopped is far worse than a logged traceback.
                logger.exception("refill tick failed")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="vakans-refill-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("refill worker stopped")


__all__ = ["RefillWorker", "TickResult", "tick"]
