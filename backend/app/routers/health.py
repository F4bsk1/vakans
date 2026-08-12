"""Liveness/readiness. Reports the applied migration revision so a half-migrated demo box is
obvious at a glance rather than surfacing as a confusing 500 mid-call."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    db_ok = False
    revision: str | None = None
    error: str | None = None

    try:
        await session.execute(text("select 1"))
        db_ok = True
        result = await session.execute(text("select version_num from alembic_version"))
        revision = result.scalar_one_or_none()
    except Exception as exc:  # healthz must report the failure, never raise it
        error = f"{type(exc).__name__}: {exc}"

    return {
        "status": "ok" if db_ok and revision else "degraded",
        "database": db_ok,
        "migration_revision": revision,
        "error": error,
    }
