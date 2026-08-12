"""Database engine and session plumbing.

Async everywhere, one engine. CLIs (`seed`, `review load`) go through `asyncio.run()` rather
than getting their own sync engine — two engines would mean two sets of transaction semantics,
and the seed/reset code would quietly diverge from the API code that step 3 reuses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.sqlalchemy_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


def make_sessionmaker(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """An independent sessionmaker for a specific settings object (tests, CLIs)."""
    engine = create_async_engine(settings.sqlalchemy_url, echo=settings.db_echo, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def unit_of_work() -> AsyncIterator[AsyncSession]:
    """Transaction boundary for non-request code (seed, CLI, background work).

    Commits on success, rolls back on exception. The events outbox is drained by the
    `after_commit` hook in `app.events`, so a rolled-back transaction never publishes a
    phantom dashboard event.
    """
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Overridden in tests to bind to the rollback-per-test session."""
    async with get_sessionmaker()() as session:
        yield session
