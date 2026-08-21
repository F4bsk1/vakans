"""The dashboard's two endpoints — spec §3: `GET /dash/{clinic_id}/state`, `WS .../events`.

The split is deliberate and is the whole reason `events` is written on every state change: the
snapshot is the only query that ever touches the calendar tables, and everything after it is a
tail of one append-only table. A dashboard left open through a demo makes exactly one expensive
request.

**Reconnect without gaps.** The snapshot returns `last_event_id`; the client opens the socket
with `?after_id=` set to it. Anything emitted in the gap between the two is replayed before the
live stream starts. Without that, an event landing during the handshake vanishes — which on a
demo looks like the cascade silently skipping a candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, status
from fastapi.websockets import WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.events import EventEnvelope, EventType, get_event_bus, recent_events
from app.models import (
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
from app.risk import risk_band
from app.schemas.dash import (
    DashCascade,
    DashClinic,
    DashDay,
    DashKpis,
    DashOffer,
    DashResource,
    DashSlot,
    DashState,
)
from app.timeutils import (
    MONTHS_SV,
    WEEKDAYS_SV,
    get_tz,
    local_day_bounds,
    now_utc,
    speak_time_sv,
    to_local,
)

logger = logging.getLogger("vakans.dash")

router = APIRouter(prefix="/dash", tags=["dash"])

# How many days the calendar shows. Five is what fits on a laptop screen without scrolling
# horizontally, which is the only screen this is ever demoed on.
CALENDAR_DAYS = 5
EVENT_BACKLOG = 60
# A WebSocket that never writes cannot tell a dropped connection from a quiet clinic, and a
# proxy will close it. Ping well inside the usual 60 s idle timeout.
PING_INTERVAL_S = 20.0


def _authorize(token: str | None, settings: Settings) -> None:
    """Read-only, so it is guarded like `/sim` rather than fail-closed like `/agent`.

    Skipped when no `VAKANS_DEMO_TOKEN` is configured, which is the local-development case.
    Accepted as a query parameter as well as a header because a browser cannot set headers on
    a WebSocket handshake, and the socket and the snapshot must not need different credentials.
    """
    if settings.demo_token is None:
        return
    if token != settings.demo_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad demo token")


async def _clinic(session: AsyncSession, clinic_id: UUID) -> Clinic:
    clinic = (
        await session.execute(select(Clinic).where(Clinic.id == clinic_id))
    ).scalar_one_or_none()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="clinic not found")
    return clinic


def _forward_business_days(start: date, count: int) -> list[date]:
    """`count` weekdays beginning with today, skipping weekends.

    Weekends are skipped rather than shown empty: the seed only generates Mon-Fri, so a
    Saturday column would be a permanently blank stripe that makes the calendar look broken.
    """
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def _day_label(d: date) -> str:
    """'mån 24 aug' — short enough for a column header, unambiguous about which Monday."""
    return f"{WEEKDAYS_SV[d.weekday()][:3]} {d.day} {MONTHS_SV[d.month - 1][:3]}"


@router.get("/{clinic_id}/state", response_model=DashState)
async def state(
    clinic_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Query()] = None,
    x_demo_token: Annotated[str | None, Header()] = None,
    # Pinned for the same reason `/sim/reset` accepts one: a rehearsal replayed against a
    # sandbox seeded for a fixed clock must agree with it about which week "this week" is.
    now: datetime | None = None,
    days: int = Query(default=CALENDAR_DAYS, ge=1, le=14),
) -> DashState:
    _authorize(token or x_demo_token, settings)
    clinic = await _clinic(session, clinic_id)

    tz = get_tz(clinic.timezone)
    at = now or now_utc()
    today = to_local(at, tz).date()
    # Today first, then forward. The obvious implementation — Monday of the current ISO week —
    # was wrong in a way only a real browser showed: run the demo on a Friday and the entire
    # calendar is history, including the slot the cascade is currently texting people about.
    # A clinic owner wants to see what is coming.
    week_dates = _forward_business_days(today, days)
    range_start, _ = local_day_bounds(week_dates[0], tz)
    _, range_end = local_day_bounds(week_dates[-1], tz)

    resources = await _resources(session, clinic.id)
    days_out = await _calendar(session, clinic, tz, week_dates, range_start, range_end, at=at)
    kpis = await _kpis(session, clinic.id, days_out, range_start, range_end)
    cascades = await _cascades(session, clinic, tz, at=at)
    events = await recent_events(session, clinic.id, limit=EVENT_BACKLOG)
    last_id = (
        await session.execute(
            select(func.coalesce(func.max(Event.id), 0)).where(Event.clinic_id == clinic.id)
        )
    ).scalar_one()

    return DashState(
        clinic=DashClinic(
            id=clinic.id,
            slug=clinic.slug,
            name=clinic.name,
            timezone=clinic.timezone,
            demo_phone=clinic.demo_phone,
            seeded_at=clinic.seeded_at,
        ),
        generated_at=at,
        last_event_id=last_id,
        range_start=week_dates[0].isoformat(),
        range_end=week_dates[-1].isoformat(),
        # Pre-rendered, like every other human-facing string: "fre 21 aug – tor 27 aug".
        range_label=f"{_day_label(week_dates[0])} – {_day_label(week_dates[-1])}",
        resources=resources,
        days=days_out,
        kpis=kpis,
        cascades=cascades,
        events=events,
    )


async def _resources(session: AsyncSession, clinic_id: UUID) -> list[DashResource]:
    rows = (
        (
            await session.execute(
                select(Resource)
                .where(Resource.clinic_id == clinic_id, Resource.active.is_(True))
                .order_by(Resource.sort_order, Resource.name)
            )
        )
        .scalars()
        .all()
    )
    return [
        DashResource(
            id=r.id,
            name=r.name,
            first_name=r.name.split()[0] if r.name.strip() else r.name,
            title=r.title,
            sort_order=r.sort_order,
        )
        for r in rows
    ]


async def _calendar(
    session: AsyncSession,
    clinic: Clinic,
    tz,
    week_dates: list[date],
    range_start: datetime,
    range_end: datetime,
    *,
    at: datetime,
) -> list[DashDay]:
    """One query for the whole week, joined out to whoever is booked into each slot.

    Outer-joined rather than queried per slot: a fortnight is ~640 slots and an N+1 here would
    turn a 40 ms snapshot into several seconds of a blank screen in front of a customer.
    """
    rows = (
        await session.execute(
            select(Slot, Booking, Patient, Service)
            .outerjoin(
                Booking,
                # Everything except `cancelled`. Joining only on `confirmed` left every past
                # slot as an anonymous grey block: elapsed bookings are `completed` or
                # `noshow`, and the no-shows are the whole point of the risk story.
                (Booking.slot_id == Slot.id) & (Booking.status != BookingStatus.CANCELLED),
            )
            .outerjoin(Patient, Booking.patient_id == Patient.id)
            .outerjoin(Service, Booking.service_id == Service.id)
            .where(
                Slot.clinic_id == clinic.id,
                Slot.starts_at >= range_start,
                Slot.starts_at <= range_end,
            )
            .order_by(Slot.starts_at)
        )
    ).all()

    by_day: dict[str, list[DashSlot]] = defaultdict(list)
    for slot, booking, patient, service in rows:
        local = to_local(slot.starts_at, tz)
        risk = float(booking.risk) if booking is not None and booking.risk is not None else None
        by_day[local.date().isoformat()].append(
            DashSlot(
                id=slot.id,
                resource_id=slot.resource_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                label=local.strftime("%H:%M"),
                status=slot.status.value,
                booking_ref=booking.booking_ref if booking else None,
                booking_status=booking.status.value if booking else None,
                patient_name=patient.name if patient else None,
                service=service.name if service else None,
                price_sek=float(service.price_sek)
                if service is not None and service.price_sek is not None
                else None,
                risk=risk,
                risk_band=risk_band(risk) if risk is not None else None,
                booked_via=booking.booked_via if booking else None,
                is_past=slot.starts_at < at,
            )
        )

    today = to_local(at, tz).date()
    return [
        DashDay(
            date=d.isoformat(),
            label=_day_label(d),
            is_today=d == today,
            slots=by_day.get(d.isoformat(), []),
        )
        for d in week_dates
    ]


async def _kpis(
    session: AsyncSession,
    clinic_id: UUID,
    days: list[DashDay],
    range_start: datetime,
    range_end: datetime,
) -> DashKpis:
    slots = [s for day in days for s in day.slots]
    open_n = sum(1 for s in slots if s.status == SlotStatus.OPEN.value)
    booked_n = sum(1 for s in slots if s.status == SlotStatus.BOOKED.value)
    held_n = sum(1 for s in slots if s.status == SlotStatus.HELD.value)
    bookable = open_n + booked_n + held_n

    bands = [s.risk_band for s in slots if s.risk_band]

    via = dict(
        (
            await session.execute(
                select(Booking.booked_via, func.count())
                .join(Slot, Booking.slot_id == Slot.id)
                .where(
                    Booking.clinic_id == clinic_id,
                    Booking.status == BookingStatus.CONFIRMED,
                    Slot.starts_at >= range_start,
                    Slot.starts_at <= range_end,
                )
                .group_by(Booking.booked_via)
            )
        ).all()
    )

    # Money comes from the events table, not from a join over bookings. `events` is the ROI
    # report by charter §2, and summing it is what makes the number on screen the same number
    # that would appear in a PDF sent afterwards.
    async def _sek(event_type: EventType) -> float:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(Event.sek_value), 0)).where(
                    Event.clinic_id == clinic_id, Event.type == event_type.value
                )
            )
        ).scalar_one()
        return float(total)

    recovered = await _sek(EventType.SLOT_REFILLED)
    # What is still on the table: freed slots minus the ones already refilled.
    freed = await _sek(EventType.SLOT_FREED)

    return DashKpis(
        slots_total=len(slots),
        slots_open=open_n,
        slots_booked=booked_n,
        slots_held=held_n,
        fill_rate=round(booked_n / bookable, 4) if bookable else 0.0,
        bookings_by_voice=via.get("voice", 0),
        bookings_by_refill=via.get("refill", 0),
        recovered_sek=recovered,
        at_risk_sek=max(freed - recovered, 0.0),
        risk_green=bands.count("green"),
        risk_yellow=bands.count("yellow"),
        risk_red=bands.count("red"),
    )


async def _cascades(
    session: AsyncSession, clinic: Clinic, tz, *, at: datetime, limit: int = 5
) -> list[DashCascade]:
    """Recent cascades, newest first, each as an ordered candidate timeline."""
    rows = (
        await session.execute(
            select(RefillOffer, Patient, Slot)
            .join(Patient, RefillOffer.patient_id == Patient.id)
            .join(Slot, RefillOffer.slot_id == Slot.id)
            .where(RefillOffer.clinic_id == clinic.id)
            .order_by(RefillOffer.sent_at.desc(), RefillOffer.candidate_n)
        )
    ).all()
    if not rows:
        return []

    grouped: dict[UUID, list[tuple]] = defaultdict(list)
    order: list[UUID] = []
    for offer, patient, slot in rows:
        if offer.cascade_id not in grouped:
            order.append(offer.cascade_id)
        grouped[offer.cascade_id].append((offer, patient, slot))

    out: list[DashCascade] = []
    for cascade_id in order[:limit]:
        entries = sorted(grouped[cascade_id], key=lambda e: e[0].candidate_n)
        slot = entries[0][2]
        resource = await session.get(Resource, slot.resource_id)
        service = await _cascade_service(session, entries[0][0])

        live = [o for o, _, _ in entries if o.status is RefillOfferStatus.SENT]
        accepted = any(o.status is RefillOfferStatus.ACCEPTED for o, _, _ in entries)
        is_live = bool(live) and live[0].expires_at > at

        out.append(
            DashCascade(
                cascade_id=cascade_id,
                slot_id=slot.id,
                starts_at=slot.starts_at,
                # Pre-rendered Swedish, same rule as everywhere else: the browser is not
                # allowed to invent "på måndag kl 8".
                slot_label=(
                    f"{speak_time_sv(slot.starts_at, tz, now=at)} "
                    f"hos {resource.name.split()[0] if resource else ''}"
                ).strip(),
                service=service.name if service else None,
                price_sek=float(service.price_sek)
                if service is not None and service.price_sek is not None
                else None,
                is_live=is_live,
                expires_at=live[0].expires_at if live else None,
                outcome="refilled" if accepted else ("live" if is_live else "exhausted"),
                offers=[
                    DashOffer(
                        candidate_n=o.candidate_n,
                        patient_name=p.name,
                        status=o.status.value,
                        sent_at=o.sent_at,
                        expires_at=o.expires_at,
                        responded_at=o.responded_at,
                    )
                    for o, p, _ in entries
                ],
            )
        )
    return out


async def _cascade_service(session: AsyncSession, offer: RefillOffer) -> Service | None:
    """The service the *offer* is for, which is the waitlist entry's, not the cancelled one's."""
    from app.models import Waitlist

    entry = await session.get(Waitlist, offer.waitlist_id)
    return await session.get(Service, entry.service_id) if entry else None


# --- the live socket ------------------------------------------------------------------------


@router.websocket("/{clinic_id}/events")
async def events_socket(
    websocket: WebSocket,
    clinic_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    token: str | None = None,
    after_id: int | None = None,
) -> None:
    """Tail this clinic's event log.

    Subscribes to the bus *before* replaying the backlog, so an event emitted during the replay
    is queued rather than dropped. The client de-duplicates on `id`, which it can because
    `events.id` is a bigserial and the outbox publishes in commit order.
    """
    settings = get_settings()
    try:
        _authorize(token, settings)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    bus = get_event_bus()

    try:
        async with bus.subscribe(clinic_id) as stream:
            if after_id is not None:
                missed = await recent_events(session, clinic_id, after_id=after_id)
                for envelope in missed:
                    await websocket.send_text(envelope.model_dump_json())
            # Hand the connection back before pumping. A dashboard left open all afternoon
            # must not hold a pool connection for the whole demo just to have replayed a
            # handful of rows at the start. FastAPI's own teardown afterwards is a no-op.
            await session.close()

            await _pump(websocket, stream)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("dashboard socket failed for clinic %s", clinic_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)


async def _pump(websocket: WebSocket, stream) -> None:
    """Forward events, and ping often enough that an idle proxy does not close the socket."""
    pending: asyncio.Task[EventEnvelope] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(stream))  # type: ignore[arg-type]
            done, _ = await asyncio.wait({pending}, timeout=PING_INTERVAL_S)
            if pending in done:
                envelope = pending.result()
                pending = None
                await websocket.send_text(envelope.model_dump_json())
            else:
                # Keep the in-flight `anext` task: cancelling it would leave the shared async
                # generator unusable, which cost an hour to diagnose once already.
                await websocket.send_text('{"type":"ping"}')
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
