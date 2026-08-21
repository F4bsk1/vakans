"""The refill cascade — spec §3 "Refill-motorn", and the beat the whole demo is built around.

A booking is cancelled, the slot goes red on the dashboard, an SMS goes to the best waitlist
candidate, ninety seconds pass, the cascade advances, someone answers JA, and the slot turns
green with a SEK figure attached. That sequence is the product demo; everything else is
supporting cast.

Four design decisions worth knowing before changing anything here:

1. **The slot is `held`, not `open`, for the duration of a cascade.** That is what schema delta
   G6 exists for. `availability` only ever returns `open` slots and `book` refuses anything
   else, so a voice caller cannot take a slot out from under a running cascade — and the
   partial unique index (G1) is the backstop if that reasoning is ever wrong.

2. **Nothing here calls a third party.** `send_sms` writes a `queued` row; `app.refill.worker`
   drains it to 46elks on its own tick. So cancelling a booking during a live phone call costs
   one local transaction, not an HTTP round trip to a Swedish SMS carrier.

3. **The timeout is swept, not slept.** There is no `asyncio.sleep(90)` holding a cascade in
   memory. Offers carry `expires_at`, and `sweep_expired` advances whatever is due. A restart
   mid-cascade therefore resumes correctly instead of stranding a held slot forever.

4. **A late JA still wins if the slot is still free.** Strictly honouring the 90 s window would
   mean a patient whose thumb was two seconds slow gets nothing while the slot sits empty —
   which is precisely the waste this product exists to remove. See `_live_offer_for`.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking_ref import new_booking_ref
from app.config import Settings
from app.events import EventType, emit
from app.models import (
    Booking,
    BookingStatus,
    Clinic,
    Patient,
    RefillOffer,
    RefillOfferStatus,
    Resource,
    Service,
    ServiceResource,
    Slot,
    SlotStatus,
    SmsDirection,
    SmsMessage,
    SmsStatus,
    Waitlist,
    WaitlistStatus,
)
from app.phone import normalize_e164
from app.refill import messages
from app.risk import RiskInput, noshow_risk
from app.sms import send_sms
from app.timeutils import get_tz

logger = logging.getLogger("vakans.refill")

# Spec §3: "Timeout 90 s → nästa kandidat." Overridable per deployment because a rehearsal in
# front of a customer sometimes wants 30.
DEFAULT_OFFER_TIMEOUT = timedelta(seconds=90)

# Below this horizon a slot counts as short notice, and `short_notice_ok` becomes a hard filter.
# Spec §3 lists the flag as an unconditional candidate criterion; that reading would exclude
# from a slot ten days out the very people who said they could not come on two hours' notice,
# which is not what the flag means.
SHORT_NOTICE_HORIZON = timedelta(hours=48)

# Ranking weights. Spec §3: "väntetid + risk (lågriskpatient först)."
MAX_WAIT_DAYS = 21.0
WAIT_WEIGHT = 0.6
RISK_WEIGHT = 0.4
# Larger than the maximum of the two weighted terms, so an exact service match always outranks
# a merely compatible one rather than being traded off against a long wait.
EXACT_SERVICE_BONUS = 1.0

CascadeOutcome = Literal[
    "offered", "no_candidates", "slot_unavailable", "already_running", "refilled"
]


class Answer(StrEnum):
    ACCEPT = "ja"
    DECLINE = "nej"
    UNKNOWN = "unknown"


# Deliberately narrow. "Ja tack, den passar perfekt" is handled by the prefix check below;
# free-form negotiation over SMS is not a thing this cascade tries to do.
_ACCEPT_WORDS = frozenset({"ja", "j", "japp", "jaa", "yes", "y", "ok", "okej", "absolut"})
_DECLINE_WORDS = frozenset({"nej", "n", "nope", "no", "stopp", "stop", "avbryt", "sluta"})


def parse_reply(body: str | None) -> Answer:
    """Classify an inbound SMS body.

    Matches on the first word only. A patient replying "Ja tack!" means yes; a patient writing
    a paragraph that happens to contain "nej" somewhere means we should not guess.
    """
    if not body:
        return Answer.UNKNOWN
    first = body.strip().lower().lstrip("﻿").split()
    if not first:
        return Answer.UNKNOWN
    word = "".join(ch for ch in first[0] if ch.isalpha())
    if word in _ACCEPT_WORDS:
        return Answer.ACCEPT
    if word in _DECLINE_WORDS:
        return Answer.DECLINE
    return Answer.UNKNOWN


# --- ranking (pure) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateInput:
    """One waitlist entry considered for a freed slot. Pure input — no ORM objects."""

    waitlist_id: UUID
    patient_id: UUID
    service_id: UUID
    name: str | None
    phone: str | None
    created_at: datetime
    risk: float
    exact_service: bool


@dataclass(frozen=True)
class Candidate:
    input: CandidateInput
    waited_days: float
    score: float


def rank_candidates(inputs: list[CandidateInput], *, now: datetime) -> list[Candidate]:
    """Best first. Pure, so the ranking can be tested without a database.

    Two terms, both normalised to 0–1 so the weights mean what they say: how long someone has
    waited, and how likely they are to actually turn up. Ranking a high-risk patient first would
    refill the slot on paper and leave it empty in the room, which is the failure this whole
    product claims to fix.
    """
    ranked: list[Candidate] = []
    for item in inputs:
        waited = max((now - item.created_at).total_seconds() / 86400.0, 0.0)
        score = (
            WAIT_WEIGHT * min(waited / MAX_WAIT_DAYS, 1.0)
            + RISK_WEIGHT * (1.0 - min(max(item.risk, 0.0), 1.0))
            + (EXACT_SERVICE_BONUS if item.exact_service else 0.0)
        )
        ranked.append(Candidate(input=item, waited_days=round(waited, 2), score=round(score, 4)))

    # waitlist_id breaks ties deterministically: two entries created in the same seeded second
    # must not reorder between runs, or "same seed, same demo" stops being true.
    ranked.sort(key=lambda c: (-c.score, str(c.input.waitlist_id)))
    return ranked


# --- cascade state ----------------------------------------------------------------------------


@dataclass(frozen=True)
class CascadeState:
    """What a cascade step did. Returned to the sim endpoints and the CLI, not to a patient."""

    outcome: CascadeOutcome
    cascade_id: UUID | None = None
    slot_id: UUID | None = None
    candidate_n: int | None = None
    offer_id: UUID | None = None
    patient_name: str | None = None
    patient_phone: str | None = None
    expires_at: datetime | None = None
    remaining_candidates: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class ReplyResult:
    accepted: bool
    answer: Answer
    reason: str
    cascade_id: UUID | None = None
    candidate_n: int | None = None
    booking_ref: str | None = None
    slot_id: UUID | None = None
    sek_value: Decimal | None = None
    duplicate: bool = False


def offer_timeout(settings: Settings) -> timedelta:
    return timedelta(seconds=settings.refill_offer_timeout_s)


# --- candidate discovery ------------------------------------------------------------------------


async def _compatible_service_ids(session: AsyncSession, slot: Slot) -> set[UUID]:
    """Every service the slot's practitioner actually performs.

    Widening beyond the cancelled service is what keeps the cascade useful on a real clinic's
    data, where the person who waits for "nybesök hos naprapat" and the person who cancelled
    "återbesök" are rarely on the same service row. Exact matches still rank strictly higher.
    """
    rows = await session.execute(
        select(ServiceResource.service_id).where(ServiceResource.resource_id == slot.resource_id)
    )
    return set(rows.scalars())


async def _gave_up_this_slot(session: AsyncSession, slot: Slot) -> set[UUID]:
    """Patients who cancelled a booking on this exact slot.

    Found by running the demo rather than by reasoning about it: the first live cascade
    offered the freed 08:00 back to the person who had just cancelled it, because she was also
    on the waitlist. Correct by every rule in spec §3 and absurd to watch. Derived from the
    booking rows rather than passed down the call chain, so it still holds on candidate #7,
    after a restart, and for a slot freed by a reschedule.
    """
    rows = await session.execute(
        select(Booking.patient_id).where(
            Booking.slot_id == slot.id, Booking.status == BookingStatus.CANCELLED
        )
    )
    return set(rows.scalars())


async def find_candidates(
    session: AsyncSession,
    clinic: Clinic,
    slot: Slot,
    *,
    service_id: UUID | None,
    now: datetime,
    exclude_waitlist_ids: set[UUID] | None = None,
) -> list[Candidate]:
    """Spec §3 step 2: service matches, the time window covers the slot, short notice is ok."""
    exclude = exclude_waitlist_ids or set()
    gave_up = await _gave_up_this_slot(session, slot)

    allowed = await _compatible_service_ids(session, slot)
    if service_id is not None:
        allowed.add(service_id)
    if not allowed:
        return []

    stmt = (
        select(Waitlist, Patient)
        .join(Patient, Waitlist.patient_id == Patient.id)
        .where(
            Waitlist.clinic_id == clinic.id,
            Waitlist.status == WaitlistStatus.ACTIVE,
            Waitlist.service_id.in_(allowed),
            # A null bound means "no preference on that side", not "never".
            (Waitlist.earliest.is_(None)) | (Waitlist.earliest <= slot.starts_at),
            (Waitlist.latest.is_(None)) | (Waitlist.latest >= slot.starts_at),
        )
    )
    if slot.starts_at - now <= SHORT_NOTICE_HORIZON:
        stmt = stmt.where(Waitlist.short_notice_ok.is_(True))

    inputs: list[CandidateInput] = []
    for entry, patient in (await session.execute(stmt)).all():
        if entry.id in exclude or patient.id in gave_up:
            continue
        # No number, no cascade. Recording an offer we cannot deliver would make the dashboard
        # claim an SMS went out that never did.
        if normalize_e164(patient.phone) is None:
            continue
        inputs.append(
            CandidateInput(
                waitlist_id=entry.id,
                patient_id=patient.id,
                service_id=entry.service_id,
                name=patient.name,
                phone=patient.phone,
                created_at=entry.created_at,
                risk=float(patient.noshow_risk or 0),
                exact_service=service_id is not None and entry.service_id == service_id,
            )
        )

    return rank_candidates(inputs, now=now)


# --- sending one offer ---------------------------------------------------------------------------


async def _send_offer(
    session: AsyncSession,
    clinic: Clinic,
    slot: Slot,
    candidate: Candidate,
    *,
    cascade_id: UUID,
    candidate_n: int,
    settings: Settings,
    now: datetime,
    remaining: int,
) -> CascadeState:
    tz = get_tz(clinic.timezone)
    service = await session.get(Service, candidate.input.service_id)
    resource = await session.get(Resource, slot.resource_id)

    offer = RefillOffer(
        id=uuid4(),
        clinic_id=clinic.id,
        slot_id=slot.id,
        waitlist_id=candidate.input.waitlist_id,
        patient_id=candidate.input.patient_id,
        cascade_id=cascade_id,
        candidate_n=candidate_n,
        status=RefillOfferStatus.SENT,
        sent_at=now,
        expires_at=now + offer_timeout(settings),
    )
    session.add(offer)

    # Take the entry out of the pool so a second cascade running at the same time cannot offer
    # the same person two different slots and collect two conflicting JAs.
    entry = await session.get(Waitlist, candidate.input.waitlist_id)
    if entry is not None:
        entry.status = WaitlistStatus.OFFERED
    await session.flush()

    body = messages.offer_sms(
        clinic_name=clinic.name,
        patient_name=candidate.input.name,
        service_name=service.name if service else "en tid",
        starts_at=slot.starts_at,
        resource_name=resource.name if resource else "",
        tz=tz,
        now=now,
        timeout=offer_timeout(settings),
    )
    sms = await send_sms(
        session,
        clinic_id=clinic.id,
        to_phone=candidate.input.phone,
        body=body,
        settings=settings,
        patient_id=candidate.input.patient_id,
        refill_offer_id=offer.id,
    )

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.REFILL_SMS_SENT,
        payload={
            "cascade_id": str(cascade_id),
            "candidate_n": candidate_n,
            "slot_id": str(slot.id),
            "starts_at": slot.starts_at.isoformat(),
            "patient_name": candidate.input.name,
            "waitlist_id": str(candidate.input.waitlist_id),
            "service": service.name if service else None,
            "expires_at": offer.expires_at.isoformat(),
            "score": candidate.score,
            "waited_days": candidate.waited_days,
            "remaining_candidates": remaining,
            "sms_id": str(sms.id) if sms else None,
        },
        correlation_id=cascade_id,
        actor="refill",
    )

    return CascadeState(
        outcome="offered",
        cascade_id=cascade_id,
        slot_id=slot.id,
        candidate_n=candidate_n,
        offer_id=offer.id,
        patient_name=candidate.input.name,
        patient_phone=candidate.input.phone,
        expires_at=offer.expires_at,
        remaining_candidates=remaining,
    )


async def _end_cascade(
    session: AsyncSession,
    clinic: Clinic,
    slot: Slot | None,
    *,
    cascade_id: UUID | None,
    reason: str,
    now: datetime,
    release_slot: bool = True,
) -> CascadeState:
    """Stop holding the slot and tell the dashboard to stop waiting.

    `refill_cascade_ended` is not in spec §6. It is here because a dashboard that is told a
    cascade started and never told it finished shows a slot blinking forever, which reads as a
    hung demo rather than as "nobody wanted it".
    """
    if slot is not None and release_slot and slot.status == SlotStatus.HELD:
        slot.status = SlotStatus.OPEN
    await session.flush()

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.REFILL_CASCADE_ENDED,
        payload={
            "cascade_id": str(cascade_id) if cascade_id else None,
            "slot_id": str(slot.id) if slot else None,
            "reason": reason,
        },
        correlation_id=cascade_id,
        actor="refill",
    )
    return CascadeState(
        outcome="refilled" if reason == "accepted" else "no_candidates",
        cascade_id=cascade_id,
        slot_id=slot.id if slot else None,
        reason=reason,
    )


# --- starting and advancing ----------------------------------------------------------------------


async def start_cascade(
    session: AsyncSession,
    clinic: Clinic,
    *,
    slot_id: UUID,
    service_id: UUID | None,
    settings: Settings,
    now: datetime,
) -> CascadeState:
    """Spec §3 steps 2–3. Called from `cancel`, and from `/sim/{clinic_id}/cancel_random`."""
    slot = (
        await session.execute(
            select(Slot).where(Slot.id == slot_id, Slot.clinic_id == clinic.id).with_for_update()
        )
    ).scalar_one_or_none()
    if slot is None or slot.status not in (SlotStatus.OPEN, SlotStatus.HELD):
        return CascadeState(outcome="slot_unavailable", slot_id=slot_id, reason="slot_unavailable")

    # A slot in the past cannot be refilled, and texting people about it is worse than doing
    # nothing at all.
    if slot.starts_at <= now:
        return CascadeState(outcome="slot_unavailable", slot_id=slot_id, reason="slot_in_past")

    live = (
        (
            await session.execute(
                select(RefillOffer).where(
                    RefillOffer.slot_id == slot.id,
                    RefillOffer.status == RefillOfferStatus.SENT,
                )
            )
        )
        .scalars()
        .first()
    )
    if live is not None:
        return CascadeState(
            outcome="already_running",
            cascade_id=live.cascade_id,
            slot_id=slot.id,
            candidate_n=live.candidate_n,
            offer_id=live.id,
            expires_at=live.expires_at,
        )

    candidates = await find_candidates(session, clinic, slot, service_id=service_id, now=now)
    cascade_id = uuid4()
    if not candidates:
        return await _end_cascade(
            session, clinic, slot, cascade_id=cascade_id, reason="no_candidates", now=now
        )

    slot.status = SlotStatus.HELD
    return await _send_offer(
        session,
        clinic,
        slot,
        candidates[0],
        cascade_id=cascade_id,
        candidate_n=1,
        settings=settings,
        now=now,
        remaining=len(candidates) - 1,
    )


async def advance_cascade(
    session: AsyncSession,
    clinic: Clinic,
    *,
    cascade_id: UUID,
    settings: Settings,
    now: datetime,
    reason: str,
) -> CascadeState:
    """Move to the next candidate, or end the cascade when there is nobody left."""
    offers = list(
        (
            await session.execute(
                select(RefillOffer)
                .where(RefillOffer.cascade_id == cascade_id, RefillOffer.clinic_id == clinic.id)
                .order_by(RefillOffer.candidate_n)
            )
        ).scalars()
    )
    if not offers:
        return CascadeState(outcome="slot_unavailable", cascade_id=cascade_id, reason="no_offers")

    slot = (
        await session.execute(select(Slot).where(Slot.id == offers[0].slot_id).with_for_update())
    ).scalar_one_or_none()
    if slot is None or slot.status == SlotStatus.BOOKED:
        return await _end_cascade(
            session, clinic, slot, cascade_id=cascade_id, reason="slot_taken", now=now
        )

    first_service_id = None
    first_entry = await session.get(Waitlist, offers[0].waitlist_id)
    if first_entry is not None:
        first_service_id = first_entry.service_id

    already = {o.waitlist_id for o in offers}
    candidates = await find_candidates(
        session,
        clinic,
        slot,
        service_id=first_service_id,
        now=now,
        exclude_waitlist_ids=already,
    )
    if not candidates:
        return await _end_cascade(
            session,
            clinic,
            slot,
            cascade_id=cascade_id,
            reason=f"exhausted_after_{reason}",
            now=now,
        )

    slot.status = SlotStatus.HELD
    return await _send_offer(
        session,
        clinic,
        slot,
        candidates[0],
        cascade_id=cascade_id,
        candidate_n=max(o.candidate_n for o in offers) + 1,
        settings=settings,
        now=now,
        remaining=len(candidates) - 1,
    )


async def sweep_expired(
    session: AsyncSession, *, settings: Settings, now: datetime, limit: int = 20
) -> list[CascadeState]:
    """Expire offers whose window has closed and advance their cascades. Driven by the worker.

    `skip_locked` so two workers (or a worker and a sim endpoint) never fight over the same
    offer — whoever gets there first handles it, the other moves on.
    """
    due = list(
        (
            await session.execute(
                select(RefillOffer)
                .where(
                    RefillOffer.status == RefillOfferStatus.SENT,
                    RefillOffer.expires_at <= now,
                )
                .order_by(RefillOffer.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )

    results: list[CascadeState] = []
    for offer in due:
        clinic = await session.get(Clinic, offer.clinic_id)
        if clinic is None:  # pragma: no cover — only reachable if a clinic is deleted mid-cascade
            continue

        offer.status = RefillOfferStatus.EXPIRED
        entry = await session.get(Waitlist, offer.waitlist_id)
        if entry is not None and entry.status == WaitlistStatus.OFFERED:
            entry.status = WaitlistStatus.ACTIVE
        await session.flush()

        await emit(
            session,
            clinic_id=clinic.id,
            type=EventType.REFILL_OFFER_EXPIRED,
            payload={
                "cascade_id": str(offer.cascade_id),
                "candidate_n": offer.candidate_n,
                "slot_id": str(offer.slot_id),
            },
            correlation_id=offer.cascade_id,
            actor="refill",
        )

        results.append(
            await advance_cascade(
                session,
                clinic,
                cascade_id=offer.cascade_id,
                settings=settings,
                now=now,
                reason="timeout",
            )
        )
    return results


# --- inbound replies ---------------------------------------------------------------------------


async def _live_offer_for(
    session: AsyncSession, *, clinic_id: UUID | None, phone: str, now: datetime
) -> RefillOffer | None:
    """The offer an inbound SMS from this number is answering.

    Accepts an already-expired offer as well as a live one. A cascade that has moved on will
    reject the acceptance later if the slot is gone, but if the slot is still sitting there
    unclaimed, honouring a JA that arrived at ninety-two seconds is strictly better for the
    clinic than an empty chair. Whoever's yes lands first wins; the losers are marked
    `superseded`, which is what that enum value is for.
    """
    stmt = (
        select(RefillOffer)
        .join(Patient, RefillOffer.patient_id == Patient.id)
        .where(
            Patient.phone == phone,
            RefillOffer.status.in_([RefillOfferStatus.SENT, RefillOfferStatus.EXPIRED]),
        )
        .order_by(RefillOffer.sent_at.desc())
        .limit(1)
    )
    if clinic_id is not None:
        stmt = stmt.where(RefillOffer.clinic_id == clinic_id)
    return (await session.execute(stmt)).scalars().first()


async def _accept(
    session: AsyncSession,
    clinic: Clinic,
    offer: RefillOffer,
    *,
    settings: Settings,
    now: datetime,
    rng: random.Random,
) -> ReplyResult:
    """Spec §3 step 4: book, confirm by SMS, emit `slot_refilled {sek_value}`."""
    tz = get_tz(clinic.timezone)
    slot = (
        await session.execute(select(Slot).where(Slot.id == offer.slot_id).with_for_update())
    ).scalar_one_or_none()
    patient = await session.get(Patient, offer.patient_id)
    entry = await session.get(Waitlist, offer.waitlist_id)

    # `entry` is gone only if the waitlist row was deleted mid-cascade (a reset, in practice).
    # Without it there is no service to book, and inventing one would put the patient in front
    # of the wrong practitioner.
    if slot is None or entry is None or slot.status not in (SlotStatus.OPEN, SlotStatus.HELD):
        offer.status = RefillOfferStatus.EXPIRED
        offer.responded_at = now
        await send_sms(
            session,
            clinic_id=clinic.id,
            to_phone=patient.phone if patient else None,
            body=messages.too_late_sms(clinic_name=clinic.name),
            settings=settings,
            patient_id=offer.patient_id,
            refill_offer_id=offer.id,
        )
        return ReplyResult(
            accepted=False,
            answer=Answer.ACCEPT,
            reason="slot_taken" if entry is not None else "waitlist_entry_gone",
            cascade_id=offer.cascade_id,
            candidate_n=offer.candidate_n,
            slot_id=offer.slot_id,
        )

    # NOTE: the freed slot's length is not re-checked against the waitlist service's duration.
    # In the sandbox every slot is one grid step, so this cannot bite; a real calendar adapter
    # has to handle "60-minute treatment into a 45-minute hole" and this is where it goes.
    service = await session.get(Service, entry.service_id)
    resource = await session.get(Resource, slot.resource_id)

    risk = noshow_risk(
        RiskInput(
            booked_at=now,
            starts_at=slot.starts_at,
            tz=tz,
            is_new_patient=False,
            prior_noshows=patient.prior_noshows if patient else 0,
        )
    )
    booking = Booking(
        id=uuid4(),
        clinic_id=clinic.id,
        slot_id=slot.id,
        service_id=entry.service_id,
        patient_id=offer.patient_id,
        status=BookingStatus.CONFIRMED,
        booked_via="refill",
        risk=Decimal(str(risk)),
        booking_ref=new_booking_ref(rng),
        booked_at=now,
    )
    session.add(booking)
    slot.status = SlotStatus.BOOKED
    offer.status = RefillOfferStatus.ACCEPTED
    offer.responded_at = now
    if entry is not None:
        entry.status = WaitlistStatus.FULFILLED

    try:
        await session.flush()
    except IntegrityError:
        # G1's partial unique index fired: a voice call committed for this slot between our
        # lock and our flush. The rollback discards the whole reply, and 46elks will not retry,
        # so the patient simply never hears back — acceptable for a demo, and the alternative
        # (a second transaction to send an apology) is not worth the complexity here.
        await session.rollback()
        logger.info("refill lost the race for slot %s", offer.slot_id)
        return ReplyResult(
            accepted=False, answer=Answer.ACCEPT, reason="slot_taken", slot_id=offer.slot_id
        )

    # Everyone else still holding a live offer for this slot is now out of the running.
    siblings = (
        await session.execute(
            select(RefillOffer).where(
                RefillOffer.cascade_id == offer.cascade_id,
                RefillOffer.id != offer.id,
                RefillOffer.status == RefillOfferStatus.SENT,
            )
        )
    ).scalars()
    for sibling in siblings:
        sibling.status = RefillOfferStatus.SUPERSEDED
        sibling_entry = await session.get(Waitlist, sibling.waitlist_id)
        if sibling_entry is not None and sibling_entry.status == WaitlistStatus.OFFERED:
            sibling_entry.status = WaitlistStatus.ACTIVE

    await send_sms(
        session,
        clinic_id=clinic.id,
        to_phone=patient.phone if patient else None,
        body=messages.confirmation_sms(
            clinic_name=clinic.name,
            service_name=service.name if service else "din tid",
            starts_at=slot.starts_at,
            resource_name=resource.name if resource else "",
            booking_ref=booking.booking_ref,
            tz=tz,
            now=now,
        ),
        settings=settings,
        patient_id=offer.patient_id,
        refill_offer_id=offer.id,
    )

    # Two events, one with money on it. `booking_created` keeps the booking feed complete;
    # `slot_refilled` carries the SEK, so the ROI counter sums exactly one row per refill
    # instead of double-counting the same recovered revenue.
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CREATED,
        payload={
            "booking_ref": booking.booking_ref,
            "service": service.name if service else None,
            "resource": resource.name if resource else None,
            "starts_at": slot.starts_at.isoformat(),
            "patient_name": patient.name if patient else None,
            "risk": risk,
            "via": "refill",
        },
        correlation_id=offer.cascade_id,
        actor="refill",
    )
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.SLOT_REFILLED,
        payload={
            "cascade_id": str(offer.cascade_id),
            "candidate_n": offer.candidate_n,
            "slot_id": str(slot.id),
            "starts_at": slot.starts_at.isoformat(),
            "booking_ref": booking.booking_ref,
            "patient_name": patient.name if patient else None,
            "service": service.name if service else None,
            "minutes_to_refill": round((now - offer.sent_at).total_seconds() / 60.0, 2),
        },
        sek_value=service.price_sek if service else None,
        correlation_id=offer.cascade_id,
        actor="refill",
    )

    return ReplyResult(
        accepted=True,
        answer=Answer.ACCEPT,
        reason="refilled",
        cascade_id=offer.cascade_id,
        candidate_n=offer.candidate_n,
        booking_ref=booking.booking_ref,
        slot_id=slot.id,
        sek_value=service.price_sek if service else None,
    )


async def record_reply(
    session: AsyncSession,
    *,
    from_phone: str,
    body: str | None,
    settings: Settings,
    now: datetime,
    clinic_id: UUID | None = None,
    provider: str = "sim",
    provider_message_id: str | None = None,
    rng: random.Random | None = None,
) -> ReplyResult:
    """Handle one inbound SMS. The 46elks webhook and `/sim/waitlist_reply` both land here.

    Idempotent on `provider_message_id`: 46elks retries a webhook it did not get a 200 from,
    and booking the same slot twice because of a redelivery would be a genuinely embarrassing
    thing to have happen in front of a clinic.
    """
    rng = rng or random.Random()
    normalized = normalize_e164(from_phone)
    if normalized is None:
        return ReplyResult(accepted=False, answer=Answer.UNKNOWN, reason="unparseable_sender")

    if provider_message_id:
        seen = (
            (
                await session.execute(
                    select(SmsMessage).where(
                        SmsMessage.provider == provider,
                        SmsMessage.provider_message_id == provider_message_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if seen is not None:
            return ReplyResult(
                accepted=False, answer=parse_reply(body), reason="duplicate", duplicate=True
            )

    offer = await _live_offer_for(session, clinic_id=clinic_id, phone=normalized, now=now)
    resolved_clinic_id = offer.clinic_id if offer else clinic_id
    if resolved_clinic_id is None:
        return ReplyResult(accepted=False, answer=parse_reply(body), reason="unknown_sender")

    # Record the inbound message before acting on it, so an exception downstream still leaves a
    # trace of what the patient actually sent.
    session.add(
        SmsMessage(
            id=uuid4(),
            clinic_id=resolved_clinic_id,
            direction=SmsDirection.IN,
            to_phone=settings.sms_sender_id,
            from_phone=normalized,
            body=body or "",
            provider=provider,
            provider_message_id=provider_message_id,
            status=SmsStatus.RECEIVED,
            patient_id=offer.patient_id if offer else None,
            refill_offer_id=offer.id if offer else None,
        )
    )
    await session.flush()

    answer = parse_reply(body)
    clinic = await session.get(Clinic, resolved_clinic_id)
    if clinic is None:  # pragma: no cover
        return ReplyResult(accepted=False, answer=answer, reason="unknown_clinic")

    if offer is None:
        await emit(
            session,
            clinic_id=clinic.id,
            type=EventType.REFILL_REPLY,
            payload={"answer": answer.value, "matched_offer": False, "from": normalized},
            actor="refill",
        )
        return ReplyResult(accepted=False, answer=answer, reason="no_open_offer")

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.REFILL_REPLY,
        payload={
            "answer": answer.value,
            "matched_offer": True,
            "cascade_id": str(offer.cascade_id),
            "candidate_n": offer.candidate_n,
            "slot_id": str(offer.slot_id),
            "late": offer.status is RefillOfferStatus.EXPIRED,
        },
        correlation_id=offer.cascade_id,
        actor="refill",
    )

    if answer is Answer.ACCEPT:
        return await _accept(session, clinic, offer, settings=settings, now=now, rng=rng)

    if answer is Answer.DECLINE:
        if offer.status is RefillOfferStatus.SENT:
            offer.status = RefillOfferStatus.DECLINED
            offer.responded_at = now
            entry = await session.get(Waitlist, offer.waitlist_id)
            if entry is not None and entry.status == WaitlistStatus.OFFERED:
                entry.status = WaitlistStatus.ACTIVE
            await session.flush()
        await send_sms(
            session,
            clinic_id=clinic.id,
            to_phone=normalized,
            body=messages.declined_sms(clinic_name=clinic.name),
            settings=settings,
            patient_id=offer.patient_id,
            refill_offer_id=offer.id,
        )
        # Do not wait out the remaining timeout: a no is information, and using it immediately
        # is the difference between refilling a slot and watching it expire.
        state = await advance_cascade(
            session,
            clinic,
            cascade_id=offer.cascade_id,
            settings=settings,
            now=now,
            reason="declined",
        )
        return ReplyResult(
            accepted=False,
            answer=answer,
            reason="declined",
            cascade_id=offer.cascade_id,
            candidate_n=state.candidate_n,
            slot_id=offer.slot_id,
        )

    await send_sms(
        session,
        clinic_id=clinic.id,
        to_phone=normalized,
        body=messages.unclear_reply_sms(clinic_name=clinic.name),
        settings=settings,
        patient_id=offer.patient_id,
        refill_offer_id=offer.id,
    )
    return ReplyResult(
        accepted=False,
        answer=answer,
        reason="unclear",
        cascade_id=offer.cascade_id,
        candidate_n=offer.candidate_n,
        slot_id=offer.slot_id,
    )


# --- the cancel -> cascade seam ----------------------------------------------------------------


async def free_slot_and_cascade(
    session: AsyncSession,
    clinic: Clinic,
    slot: Slot,
    service: Service | None,
    *,
    settings: Settings,
    now: datetime,
    actor: str = "agent",
) -> CascadeState:
    """Emit `slot_freed` and immediately start a cascade. Spec §3 step 1.

    Both halves belong together: the dashboard's red-blinking slot and the first SMS are the
    same moment of the demo, and splitting them across a poll interval makes the pause visible.
    """
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.SLOT_FREED,
        payload={
            "slot_id": str(slot.id),
            "starts_at": slot.starts_at.isoformat(),
            "service_id": str(service.id) if service else None,
            "service": service.name if service else None,
        },
        # The price rides along so the dashboard can show what is at stake the instant the slot
        # goes red, before anyone has replied.
        sek_value=service.price_sek if service else None,
        actor=actor,
    )
    return await start_cascade(
        session,
        clinic,
        slot_id=slot.id,
        service_id=service.id if service else None,
        settings=settings,
        now=now,
    )


__all__ = [
    "Answer",
    "Candidate",
    "CandidateInput",
    "CascadeState",
    "ReplyResult",
    "advance_cascade",
    "find_candidates",
    "free_slot_and_cascade",
    "parse_reply",
    "rank_candidates",
    "record_reply",
    "start_cascade",
    "sweep_expired",
]
