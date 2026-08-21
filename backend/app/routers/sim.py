"""Demo choreography — spec §3 "Demo-regi".

`/sim/*` is destructive by design and is the endpoint run between customer meetings, so it is
token-guarded and takes an advisory lock.

Three endpoints, in the order a demo uses them: `reset` arms the sandbox, `cancel_random` opens
a hole and starts the refill cascade, `waitlist_reply` answers on a candidate's behalf when the
real SMS path is not available.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking_ref import normalize_booking_ref
from app.config import Settings, get_settings
from app.db import get_session, unit_of_work
from app.events import EventType, emit
from app.models import (
    CLINIC_SCOPED_DELETE_ORDER,
    Booking,
    BookingStatus,
    Clinic,
    Event,
    Patient,
    RefillOffer,
    RefillOfferStatus,
    Resource,
    Service,
    Slot,
    SlotStatus,
)
from app.refill import engine
from app.schemas.sim import (
    CancelRandomRequest,
    CancelRandomResponse,
    CascadeStateOut,
    ClinicSummary,
    DemoSlotOut,
    ResetRequest,
    ResetResponse,
    WaitlistReplyRequest,
    WaitlistReplyResponse,
)
from app.seed import SeedConfig, seed_clinic
from app.timeutils import get_tz, now_utc, speak_time_sv

logger = logging.getLogger("vakans.sim")


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


@router.post("/{clinic_id}/cancel_random", response_model=CancelRandomResponse)
async def cancel_random(
    clinic_id: UUID,
    body: CancelRandomRequest | None = None,
    session: Annotated[AsyncSession, Depends(get_session)] = None,  # type: ignore[assignment]
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> CancelRandomResponse:
    """Cancel a seeded booking and start the cascade — the opening move of the demo.

    Not actually random by default: it prefers a booking whose freed slot has live waitlist
    candidates. A cascade with nobody to text is a correct outcome and a dead demo, and which
    one you get should be a choice rather than a dice roll.
    """
    body = body or CancelRandomRequest()
    now = body.now or now_utc()
    clinic = await _require_clinic(session, clinic_id)
    tz = get_tz(clinic.timezone)

    stmt = (
        select(Booking, Slot, Service, Patient, Resource)
        .join(Slot, Booking.slot_id == Slot.id)
        .join(Service, Booking.service_id == Service.id)
        .join(Patient, Booking.patient_id == Patient.id)
        .join(Resource, Slot.resource_id == Resource.id)
        .where(
            Booking.clinic_id == clinic_id,
            Booking.status == BookingStatus.CONFIRMED,
            Slot.starts_at > now,
        )
        .order_by(Slot.starts_at)
        .limit(40)
    )
    if body.booking_ref:
        stmt = stmt.where(Booking.booking_ref == normalize_booking_ref(body.booking_ref))

    rows = (await session.execute(stmt)).all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no upcoming confirmed booking to cancel — run reset first",
        )

    chosen = rows[0]
    if body.prefer_with_candidates and not body.booking_ref:
        for row in rows:
            candidates = await engine.find_candidates(
                session, clinic, row[1], service_id=row[2].id, now=now
            )
            if candidates:
                chosen = row
                break

    booking, slot, service, patient, resource = chosen

    booking.status = BookingStatus.CANCELLED
    slot.status = SlotStatus.OPEN
    await session.flush()
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CANCELLED,
        payload={
            "booking_ref": booking.booking_ref,
            "service": service.name,
            "patient_name": patient.name,
            "starts_at": slot.starts_at.isoformat(),
        },
        actor="sim",
    )
    cascade = await engine.free_slot_and_cascade(
        session, clinic, slot, service, settings=settings, now=now, actor="sim"
    )
    await session.commit()

    return CancelRandomResponse(
        booking_ref=booking.booking_ref,
        patient_name=patient.name,
        service=service.name,
        resource_name=resource.name,
        starts_at=slot.starts_at,
        speech=f"{speak_time_sv(slot.starts_at, tz, now=now)} hos {resource.name.split()[0]}",
        sek_at_risk=float(service.price_sek) if service.price_sek is not None else None,
        cascade=_cascade_out(cascade),
    )


@router.post("/{clinic_id}/waitlist_reply", response_model=WaitlistReplyResponse)
async def waitlist_reply(
    clinic_id: UUID,
    body: WaitlistReplyRequest | None = None,
    session: Annotated[AsyncSession, Depends(get_session)] = None,  # type: ignore[assignment]
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> WaitlistReplyResponse:
    """Answer an outstanding offer on a candidate's behalf.

    Spec §3 is explicit that this is the *fallback*: the JA-svararen holds your own mobile and
    is supposed to reply for real over the 46elks webhook. This exists so a demo still lands
    when the venue has no signal, or when the founder is the one talking.
    """
    body = body or WaitlistReplyRequest()
    await _require_clinic(session, clinic_id)

    stmt = (
        select(RefillOffer, Patient)
        .join(Patient, RefillOffer.patient_id == Patient.id)
        .where(
            RefillOffer.clinic_id == clinic_id,
            RefillOffer.status == RefillOfferStatus.SENT,
        )
        .order_by(RefillOffer.sent_at.desc())
    )
    if body.cascade_id is not None:
        stmt = stmt.where(RefillOffer.cascade_id == body.cascade_id)
    if body.candidate_n is not None:
        stmt = stmt.where(RefillOffer.candidate_n == body.candidate_n)

    found = (await session.execute(stmt)).first()
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no outstanding refill offer"
        )
    offer, patient = found

    if body.delay_s:
        # Fire and forget on its own session: the request must return immediately so the
        # operator can watch the dashboard rather than a spinner. Demo choreography only —
        # nothing in the product path schedules work this way.
        _schedule_reply(
            clinic_id=clinic_id,
            phone=patient.phone or "",
            answer=body.answer,
            delay_s=body.delay_s,
        )
        return WaitlistReplyResponse(
            scheduled_in_s=body.delay_s,
            cascade_id=offer.cascade_id,
            candidate_n=offer.candidate_n,
            patient_name=patient.name,
        )

    result = await engine.record_reply(
        session,
        clinic_id=clinic_id,
        from_phone=patient.phone or "",
        body=body.answer,
        settings=settings,
        now=body.now or now_utc(),
        provider="sim",
    )
    await session.commit()

    return WaitlistReplyResponse(
        accepted=result.accepted,
        answer=result.answer.value,
        reason=result.reason,
        cascade_id=result.cascade_id or offer.cascade_id,
        candidate_n=result.candidate_n,
        patient_name=patient.name,
        booking_ref=result.booking_ref,
        sek_value=float(result.sek_value) if result.sek_value is not None else None,
    )


def _schedule_reply(*, clinic_id: UUID, phone: str, answer: str, delay_s: int) -> None:
    async def _later() -> None:
        await asyncio.sleep(delay_s)
        try:
            async with unit_of_work() as session:
                await engine.record_reply(
                    session,
                    clinic_id=clinic_id,
                    from_phone=phone,
                    body=answer,
                    settings=get_settings(),
                    now=now_utc(),
                    provider="sim",
                )
        except Exception:
            # A failed rehearsal reply must not take the API down with it.
            logger.exception("delayed waitlist reply failed for clinic %s", clinic_id)

    task = asyncio.create_task(_later(), name=f"vakans-sim-reply-{clinic_id}")
    # Hold a reference: asyncio only keeps a weak one, and a garbage-collected task simply
    # never runs — which would look exactly like "the JA never arrived".
    _PENDING_REPLIES.add(task)
    task.add_done_callback(_PENDING_REPLIES.discard)


_PENDING_REPLIES: set[asyncio.Task[None]] = set()


def _cascade_out(state: engine.CascadeState) -> CascadeStateOut:
    return CascadeStateOut(
        outcome=state.outcome,
        cascade_id=state.cascade_id,
        slot_id=state.slot_id,
        candidate_n=state.candidate_n,
        patient_name=state.patient_name,
        expires_at=state.expires_at,
        remaining_candidates=state.remaining_candidates,
        reason=state.reason,
    )


async def _require_clinic(session: AsyncSession, clinic_id: UUID) -> Clinic:
    clinic = (
        await session.execute(select(Clinic).where(Clinic.id == clinic_id))
    ).scalar_one_or_none()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="clinic not found")
    return clinic


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
