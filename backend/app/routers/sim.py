"""Demo choreography — spec §3 "Demo-regi".

`/sim/*` is destructive by design and is the endpoint run between customer meetings, so it is
token-guarded and takes an advisory lock. Step 3 adds `cancel_random` and `waitlist_reply`
here; step 1 ships `reset` plus a listing endpoint.
"""

from __future__ import annotations

import time as _time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.events import EventType, emit
from app.models import CLINIC_SCOPED_DELETE_ORDER, Clinic, Event, Resource, Slot
from app.schemas.sim import ClinicSummary, DemoSlotOut, ResetRequest, ResetResponse
from app.seed import SeedConfig, seed_clinic
from app.timeutils import get_tz, now_utc, speak_time_sv


async def require_demo_token(
    x_demo_token: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> None:
    """Guard the destructive endpoints.

    Skipped in local development and under test when no token is configured; required in the
    `demo` environment, which is the one a clinic owner can actually reach. Ten lines that stop
    a curious visitor from resetting a demo that is mid-meeting.
    """
    if settings.demo_token is None:
        if settings.env in ("local", "test"):
            return
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="VAKANS_DEMO_TOKEN must be set when env=demo",
        )
    if x_demo_token != settings.demo_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad demo token")


router = APIRouter(prefix="/sim", tags=["sim"], dependencies=[Depends(require_demo_token)])


@router.get("/clinics", response_model=list[ClinicSummary])
async def list_clinics(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ClinicSummary]:
    """Five-second check that multi-clinic isolation is intact (decision D5)."""
    rows = (await session.execute(select(Clinic).order_by(Clinic.slug))).scalars().all()
    return [
        ClinicSummary(
            id=c.id,
            slug=c.slug,
            name=c.name,
            timezone=c.timezone,
            demo_phone=c.demo_phone,
            seeded_at=c.seeded_at,
            seed_fingerprint=c.seed_fingerprint,
            profile_source=(c.profile or {}).get("meta", {}).get("source"),
            profile_reviewed_at=c.profile_reviewed_at,
        )
        for c in rows
    ]


@router.post("/{clinic_id}/reset", response_model=ResetResponse)
async def reset(
    clinic_id: UUID,
    body: ResetRequest | None = None,
    session: Annotated[AsyncSession, Depends(get_session)] = None,  # type: ignore[assignment]
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> ResetResponse:
    """Restore this clinic's sandbox to a freshly seeded state.

    Everything below happens in one transaction, so a failure part-way leaves the previous
    sandbox untouched rather than half-deleted.
    """
    started = _time.perf_counter()
    body = body or ResetRequest()

    # Serialise concurrent resets: two dashboards clicking at once, or a reset landing during a
    # live call, must not interleave. Released automatically at commit or rollback.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(clinic_id), 0)))
    )

    clinic = (
        await session.execute(select(Clinic).where(Clinic.id == clinic_id).with_for_update())
    ).scalar_one_or_none()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="clinic not found")

    # The QA gate with teeth. Spec §5 calls the manual review "obligatorisk"; this is what makes
    # it so. A scraped profile nobody has checked physically cannot be demoed.
    meta = (clinic.profile or {}).get("meta", {})
    if meta.get("source") == "ingestion" and clinic.profile_reviewed_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "profile_not_reviewed",
                "message": (
                    "This profile came from ingestion and has not been reviewed. "
                    f"Run: vakans-review show {clinic.slug} && vakans-review approve {clinic.slug}"
                ),
            },
        )

    # Clinic-scoped deletes, children before parents. The order lives next to the models so a
    # new table cannot be added without this list appearing in the same diff.
    for model in CLINIC_SCOPED_DELETE_ORDER:
        if model is Event and body.keep_events:
            continue
        await session.execute(delete(model).where(model.clinic_id == clinic_id))
    # NOTE: the clinics row itself is never touched. Reset restores the sandbox; it does not
    # un-ingest the clinic, so profile, slug, demo_phone and review metadata all survive.

    now = body.now or now_utc()
    overrides = {
        k: v
        for k, v in {
            "rng_seed": body.seed,
            "booked_ratio": body.booked_ratio,
            "guaranteed_open": body.guaranteed_open,
            "waitlist_size": body.waitlist_size,
        }.items()
        if v is not None
    }
    cfg = SeedConfig(founder_phone=settings.founder_mobile, **overrides)

    result = await seed_clinic(session, clinic, now=now, cfg=cfg)

    clinic.seeded_at = now
    clinic.seed_fingerprint = result.fingerprint

    demo_slots = await _demo_slot_details(session, clinic, result.demo_slot_ids, now=now)

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.SIM_RESET,
        payload={
            "counts": result.counts,
            "fingerprint": result.fingerprint,
            "demo_slot_strategy": result.demo_slot_strategy,
            "seed": cfg.rng_seed,
        },
        actor="sim",
    )
    await session.commit()

    return ResetResponse(
        clinic_id=clinic.id,
        slug=clinic.slug,
        fingerprint=result.fingerprint,
        counts=result.counts,
        demo_slots=demo_slots,
        demo_slot_strategy=result.demo_slot_strategy,
        founder_waitlist_id=result.founder_waitlist_id,
        risk_band_counts=result.risk_band_counts,
        duration_ms=int((_time.perf_counter() - started) * 1000),
    )


async def _demo_slot_details(
    session: AsyncSession, clinic: Clinic, slot_ids: list[UUID], *, now
) -> list[DemoSlotOut]:
    if not slot_ids:
        return []
    tz = get_tz(clinic.timezone)
    rows = (
        await session.execute(
            select(Slot, Resource)
            .join(Resource, Slot.resource_id == Resource.id)
            .where(Slot.id.in_(slot_ids))
            .order_by(Slot.starts_at)
        )
    ).all()
    return [
        DemoSlotOut(
            id=slot.id,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            resource_name=resource.name,
            # Pre-formatted here for the same reason the agent tools do it: the backend owns
            # Swedish time rendering, never the LLM.
            speech=(f"{speak_time_sv(slot.starts_at, tz, now=now)} hos {resource.name.split()[0]}"),
        )
        for slot, resource in rows
    ]
