"""The agent's tools — spec §3.

Every function returns `ToolResult{speech, data}`: the backend formats what the agent says, so
the LLM never renders a Swedish time or a price. That is the charter's latency trick and it
also removes a class of hallucination.

The business logic lives here, not in the router, so it is testable without HTTP and so the
step-4 voice adapter stays thin (charter D1 — the whole point is that swapping Retell for
Pipecat touches nothing below this line).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import safety, speech
from app.agent.matching import best_match, score_text
from app.booking_ref import new_booking_ref, normalize_booking_ref
from app.config import Settings
from app.events import EventType, emit
from app.models import (
    Booking,
    BookingStatus,
    Clinic,
    Patient,
    Resource,
    Service,
    ServiceResource,
    Slot,
    SlotStatus,
    Waitlist,
    WaitlistStatus,
)
from app.phone import normalize_e164, same_number
from app.risk import RiskInput, noshow_risk
from app.schemas.clinic_profile import ClinicProfile
from app.sms import send_sms
from app.timeutils import get_tz, speak_time_sv

logger = logging.getLogger("vakans.agent")

MAX_OPTIONS = 3  # spec §4 rule 1: never read more than three alternatives
KNOWLEDGE_CONFIDENCE_FLOOR = 0.34  # below this the agent promises a callback instead


@dataclass
class ToolResult:
    """The canonical agent-tool return shape (charter §4 coding conventions)."""

    speech: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"speech": self.speech, "data": self.data}


def _profile(clinic: Clinic) -> ClinicProfile:
    return ClinicProfile.model_validate(clinic.profile["profile"])


async def _services(session: AsyncSession, clinic_id: UUID) -> list[Service]:
    rows = await session.execute(
        select(Service).where(Service.clinic_id == clinic_id, Service.bookable.is_(True))
    )
    return list(rows.scalars())


async def _resources(session: AsyncSession, clinic_id: UUID) -> list[Resource]:
    rows = await session.execute(
        select(Resource).where(Resource.clinic_id == clinic_id, Resource.active.is_(True))
    )
    return list(rows.scalars())


# --- availability -----------------------------------------------------------------------------


async def availability(
    session: AsyncSession,
    clinic: Clinic,
    *,
    service_name: str | None = None,
    resource_name: str | None = None,
    earliest: datetime | None = None,
    latest: datetime | None = None,
    now: datetime,
) -> ToolResult:
    """Top three open slots, phrased for reading aloud."""
    verdict = safety.check(service_name, resource_name)
    if verdict.is_blocked:
        return await _escalate_for(session, clinic, verdict, now=now, source="availability")

    tz = get_tz(clinic.timezone)
    services = await _services(session, clinic.id)
    resources = await _resources(session, clinic.id)

    service_match = best_match(service_name, services, key=lambda s: s.name)
    resource_match = best_match(resource_name, resources, key=lambda r: r.name)

    if service_name and (service_match is None or not service_match.is_confident):
        return ToolResult(
            speech.UNKNOWN_SERVICE_SPEECH,
            {
                "matched": False,
                "did_you_mean": [s.name for s in services[:MAX_OPTIONS]],
            },
        )

    service = service_match.item if service_match and service_match.is_confident else None
    resource = resource_match.item if resource_match and resource_match.is_confident else None

    stmt = (
        select(Slot, Resource)
        .join(Resource, Slot.resource_id == Resource.id)
        .where(
            Slot.clinic_id == clinic.id,
            Slot.status == SlotStatus.OPEN,
            # A slot the caller could not physically reach is worse than no slot at all.
            Slot.starts_at >= (earliest or now + timedelta(minutes=90)),
        )
        .order_by(Slot.starts_at)
        .limit(60)
    )
    if latest is not None:
        stmt = stmt.where(Slot.starts_at <= latest)
    if resource is not None:
        stmt = stmt.where(Slot.resource_id == resource.id)
    if service is not None:
        # Only practitioners who actually perform this service.
        allowed = select(ServiceResource.resource_id).where(
            ServiceResource.service_id == service.id
        )
        stmt = stmt.where(Slot.resource_id.in_(allowed))

    rows = (await session.execute(stmt)).all()

    # Distinct start times: four practitioners free at 08:00 is one option to offer, not four.
    picked: list[tuple[Slot, Resource]] = []
    seen: set[datetime] = set()
    for slot, res in rows:
        if slot.starts_at in seen:
            continue
        seen.add(slot.starts_at)
        picked.append((slot, res))
        if len(picked) == MAX_OPTIONS:
            break

    spoken = speech.speak_availability(
        [(s.starts_at, r.name) for s, r in picked],
        tz,
        now=now,
        service_name=service.name if service else None,
    )
    return ToolResult(
        spoken,
        {
            "service_id": str(service.id) if service else None,
            "service_name": service.name if service else None,
            "price_sek": float(service.price_sek) if service and service.price_sek else None,
            "slots": [
                {
                    "slot_id": str(s.id),
                    "starts_at": s.starts_at.isoformat(),
                    "resource_name": r.name,
                    "speech": speak_time_sv(s.starts_at, tz, now=now),
                }
                for s, r in picked
            ],
        },
    )


# --- book -------------------------------------------------------------------------------------


async def _find_or_create_patient(
    session: AsyncSession, clinic: Clinic, *, name: str | None, phone: str | None, now: datetime
) -> tuple[Patient, bool]:
    """Returns (patient, is_new). Keyed on phone — see delta G5 in docs/SCHEMA_NOTES.md."""
    normalized = normalize_e164(phone)
    if normalized:
        existing = (
            await session.execute(
                select(Patient).where(Patient.clinic_id == clinic.id, Patient.phone == normalized)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if name and not existing.name:
                existing.name = name
            return existing, False

    patient = Patient(
        id=uuid4(),
        clinic_id=clinic.id,
        name=name,
        phone=normalized,
        is_demo=True,
        first_seen_at=now,
        created_at=now,
    )
    session.add(patient)
    await session.flush()
    return patient, True


async def book(
    session: AsyncSession,
    clinic: Clinic,
    *,
    slot_id: UUID,
    service_id: UUID,
    patient_name: str | None,
    phone: str | None,
    settings: Settings,
    now: datetime,
    rng: random.Random | None = None,
) -> ToolResult:
    """Claim a slot. Handles the race the demo deliberately creates."""
    tz = get_tz(clinic.timezone)
    rng = rng or random.Random()

    # Lock the slot before reading its status, so a concurrent voice call and refill "JA"
    # serialise here rather than both seeing `open`. The partial unique index (delta G1) is
    # the backstop if this is ever bypassed.
    slot = (
        await session.execute(
            select(Slot).where(Slot.id == slot_id, Slot.clinic_id == clinic.id).with_for_update()
        )
    ).scalar_one_or_none()

    if slot is None:
        return ToolResult(speech.SLOT_TAKEN_SPEECH, {"booked": False, "reason": "slot_not_found"})
    if slot.status not in (SlotStatus.OPEN, SlotStatus.HELD):
        return ToolResult(speech.SLOT_TAKEN_SPEECH, {"booked": False, "reason": "slot_taken"})

    service = (
        await session.execute(
            select(Service).where(Service.id == service_id, Service.clinic_id == clinic.id)
        )
    ).scalar_one_or_none()
    if service is None:
        return ToolResult(speech.UNKNOWN_SERVICE_SPEECH, {"booked": False, "reason": "no_service"})

    resource = (
        await session.execute(select(Resource).where(Resource.id == slot.resource_id))
    ).scalar_one()
    patient, is_new = await _find_or_create_patient(
        session, clinic, name=patient_name, phone=phone, now=now
    )

    risk = noshow_risk(
        RiskInput(
            booked_at=now,
            starts_at=slot.starts_at,
            tz=tz,
            is_new_patient=is_new,
            prior_noshows=patient.prior_noshows,
        )
    )

    booking = Booking(
        id=uuid4(),
        clinic_id=clinic.id,
        slot_id=slot.id,
        service_id=service.id,
        patient_id=patient.id,
        status=BookingStatus.CONFIRMED,
        booked_via="voice",
        risk=Decimal(str(risk)),
        booking_ref=new_booking_ref(rng),
        booked_at=now,
    )
    session.add(booking)
    slot.status = SlotStatus.BOOKED
    patient.noshow_risk = Decimal(str(risk))

    try:
        await session.flush()
    except IntegrityError:
        # Lost the race despite the lock (or a duplicate booking_ref). Either way the honest
        # answer is the same, and the agent reads it verbatim.
        await session.rollback()
        logger.info("book lost the race for slot %s", slot_id)
        return ToolResult(speech.SLOT_TAKEN_SPEECH, {"booked": False, "reason": "slot_taken"})

    spoken = speech.speak_booking_confirmation(
        service_name=service.name,
        starts_at=slot.starts_at,
        resource_name=resource.name,
        price=service.price_sek,
        booking_ref=booking.booking_ref,
        tz=tz,
        now=now,
    )

    sms_body = (
        f"{clinic.name}: {service.name} {speak_time_sv(slot.starts_at, tz, now=now)} "
        f"hos {speech.first_name(resource.name)}. Ref {booking.booking_ref}. "
        f"Avboka senast 24h innan."
    )
    sms = await send_sms(
        session,
        clinic_id=clinic.id,
        to_phone=patient.phone,
        body=sms_body,
        settings=settings,
        patient_id=patient.id,
    )

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CREATED,
        payload={
            "booking_ref": booking.booking_ref,
            "service": service.name,
            "resource": resource.name,
            "starts_at": slot.starts_at.isoformat(),
            "patient_name": patient.name,
            "risk": risk,
            "is_new_patient": is_new,
            "sms_id": str(sms.id) if sms else None,
        },
        sek_value=service.price_sek,
        actor="agent",
    )

    return ToolResult(
        spoken,
        {
            "booked": True,
            "booking_ref": booking.booking_ref,
            "starts_at": slot.starts_at.isoformat(),
            "service_name": service.name,
            "resource_name": resource.name,
            "price_sek": float(service.price_sek) if service.price_sek else None,
            "risk": risk,
        },
    )


# --- lookup, cancel, reschedule -----------------------------------------------------------------


async def _find_booking(
    session: AsyncSession,
    clinic: Clinic,
    *,
    booking_ref: str | None,
    phone: str | None,
    name: str | None,
) -> Booking | None:
    """Locate a confirmed booking.

    Spec §4 rule 4 requires name + the phone the booking was made with before touching an
    existing booking, so a reference alone is never sufficient — it must agree with the phone.
    """
    stmt = select(Booking).where(
        Booking.clinic_id == clinic.id, Booking.status == BookingStatus.CONFIRMED
    )

    if booking_ref:
        candidate = (
            await session.execute(
                stmt.where(Booking.booking_ref == normalize_booking_ref(booking_ref))
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None
        patient = await session.get(Patient, candidate.patient_id)
        if phone and patient and not same_number(patient.phone, phone):
            return None  # reference is right but the caller is not the patient
        return candidate

    normalized = normalize_e164(phone)
    if not normalized:
        return None

    rows = (
        await session.execute(
            stmt.join(Patient, Booking.patient_id == Patient.id)
            .where(Patient.phone == normalized)
            .order_by(Booking.created_at.desc())
        )
    ).scalars()
    matches = list(rows)
    if not matches:
        return None
    if name:
        # One phone can front for a family (delta G5's known simplification) — prefer the
        # booking whose patient name the caller actually gave.
        for booking in matches:
            patient = await session.get(Patient, booking.patient_id)
            if patient and score_text(name, patient.name) >= 0.5:
                return booking
    return matches[0]


async def cancel(
    session: AsyncSession,
    clinic: Clinic,
    *,
    booking_ref: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    now: datetime,
) -> ToolResult:
    """Cancel and free the slot. The refill cascade hangs off the `slot_freed` event."""
    tz = get_tz(clinic.timezone)
    booking = await _find_booking(session, clinic, booking_ref=booking_ref, phone=phone, name=name)
    if booking is None:
        return ToolResult(speech.NOT_FOUND_SPEECH, {"cancelled": False, "reason": "not_found"})

    slot = await session.get(Slot, booking.slot_id)
    service = await session.get(Service, booking.service_id)
    profile = _profile(clinic)

    booking.status = BookingStatus.CANCELLED
    if slot is not None:
        slot.status = SlotStatus.OPEN
    await session.flush()

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CANCELLED,
        payload={"booking_ref": booking.booking_ref, "service": service.name if service else None},
        actor="agent",
    )
    if slot is not None:
        # The refill engine (step 3b) subscribes to this. Carrying the price means the
        # dashboard's SEK counter can show what is at stake the moment the slot goes red.
        await emit(
            session,
            clinic_id=clinic.id,
            type=EventType.SLOT_FREED,
            payload={
                "slot_id": str(slot.id),
                "starts_at": slot.starts_at.isoformat(),
                "service_id": str(service.id) if service else None,
            },
            sek_value=service.price_sek if service else None,
            actor="agent",
        )

    spoken = speech.speak_cancellation(
        service_name=service.name if service else "din tid",
        starts_at=slot.starts_at if slot else now,
        tz=tz,
        now=now,
        policy=profile.policies.cancellation,
    )
    return ToolResult(
        spoken,
        {
            "cancelled": True,
            "booking_ref": booking.booking_ref,
            "slot_id": str(slot.id) if slot else None,
        },
    )


async def reschedule(
    session: AsyncSession,
    clinic: Clinic,
    *,
    new_slot_id: UUID,
    booking_ref: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    settings: Settings,
    now: datetime,
) -> ToolResult:
    tz = get_tz(clinic.timezone)
    booking = await _find_booking(session, clinic, booking_ref=booking_ref, phone=phone, name=name)
    if booking is None:
        return ToolResult(speech.NOT_FOUND_SPEECH, {"rescheduled": False, "reason": "not_found"})

    new_slot = (
        await session.execute(
            select(Slot)
            .where(Slot.id == new_slot_id, Slot.clinic_id == clinic.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if new_slot is None or new_slot.status not in (SlotStatus.OPEN, SlotStatus.HELD):
        return ToolResult(speech.SLOT_TAKEN_SPEECH, {"rescheduled": False, "reason": "slot_taken"})

    old_slot = await session.get(Slot, booking.slot_id)
    service = await session.get(Service, booking.service_id)
    resource = await session.get(Resource, new_slot.resource_id)

    booking.slot_id = new_slot.id
    new_slot.status = SlotStatus.BOOKED
    if old_slot is not None:
        old_slot.status = SlotStatus.OPEN

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return ToolResult(speech.SLOT_TAKEN_SPEECH, {"rescheduled": False, "reason": "slot_taken"})

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_RESCHEDULED,
        payload={
            "booking_ref": booking.booking_ref,
            "from": old_slot.starts_at.isoformat() if old_slot else None,
            "to": new_slot.starts_at.isoformat(),
        },
        actor="agent",
    )
    if old_slot is not None:
        await emit(
            session,
            clinic_id=clinic.id,
            type=EventType.SLOT_FREED,
            payload={"slot_id": str(old_slot.id), "starts_at": old_slot.starts_at.isoformat()},
            sek_value=service.price_sek if service else None,
            actor="agent",
        )

    patient = await session.get(Patient, booking.patient_id)
    await send_sms(
        session,
        clinic_id=clinic.id,
        to_phone=patient.phone if patient else None,
        body=(
            f"{clinic.name}: din tid är flyttad till "
            f"{speak_time_sv(new_slot.starts_at, tz, now=now)}. Ref {booking.booking_ref}."
        ),
        settings=settings,
        patient_id=patient.id if patient else None,
    )

    spoken = speech.speak_reschedule(
        service_name=service.name if service else "din tid",
        starts_at=new_slot.starts_at,
        resource_name=resource.name if resource else "",
        tz=tz,
        now=now,
    )
    return ToolResult(
        spoken,
        {
            "rescheduled": True,
            "booking_ref": booking.booking_ref,
            "starts_at": new_slot.starts_at.isoformat(),
        },
    )


# --- waitlist ---------------------------------------------------------------------------------


async def add_to_waitlist(
    session: AsyncSession,
    clinic: Clinic,
    *,
    service_id: UUID,
    patient_name: str | None,
    phone: str | None,
    earliest: datetime | None = None,
    latest: datetime | None = None,
    now: datetime,
) -> ToolResult:
    service = await session.get(Service, service_id)
    if service is None or service.clinic_id != clinic.id:
        return ToolResult(speech.UNKNOWN_SERVICE_SPEECH, {"waitlisted": False})

    patient, _ = await _find_or_create_patient(
        session, clinic, name=patient_name, phone=phone, now=now
    )
    entry = Waitlist(
        id=uuid4(),
        clinic_id=clinic.id,
        patient_id=patient.id,
        service_id=service.id,
        earliest=earliest or now,
        latest=latest or now + timedelta(days=30),
        short_notice_ok=True,
        created_at=now,
        status=WaitlistStatus.ACTIVE,
    )
    session.add(entry)
    await session.flush()

    return ToolResult(
        speech.speak_waitlisted(service.name),
        {"waitlisted": True, "waitlist_id": str(entry.id)},
    )


# --- knowledge --------------------------------------------------------------------------------


@dataclass(frozen=True)
class KnowledgeItem:
    topic: str
    keywords: str  # what a caller might actually say
    answer: str  # what the agent says back


def _knowledge_candidates(profile: ClinicProfile) -> list[KnowledgeItem]:
    """Answerable topics drawn from the reviewed profile.

    Keywords are kept **separate from the answer body**, and matching scores against the
    keywords only. Folding the two together looks tidier and is wrong: "Mottagningen *ligger*
    på bottenplan" made the accessibility note the top hit for "var ligger ni?", beating the
    actual address. One incidental verb should not decide what the agent says.

    No vector store — a clinic profile is a few KB, and lexical scoring is instant, free and
    deterministic. See docs/SCHEMA_NOTES.md: knowledge_chunks was deliberately deferred.
    """
    out: list[KnowledgeItem] = []

    # A FAQ question *is* the keyword set — it is already phrased the way callers ask.
    out.extend(KnowledgeItem("faq", item.q, item.a) for item in profile.faq)

    if profile.policies.cancellation:
        out.append(
            KnowledgeItem(
                "avbokning",
                "avboka avbokning avbokningsregler ombokning ångra återbud",
                profile.policies.cancellation,
            )
        )
    if profile.policies.late_fee:
        out.append(
            KnowledgeItem(
                "uteblivet",
                "uteblivet utebliven missa missade straffavgift debiteras",
                profile.policies.late_fee,
            )
        )
    if profile.policies.insurance_accepted:
        listed = speech.join_sv(list(profile.policies.insurance_accepted))
        out.append(
            KnowledgeItem(
                "försäkring",
                f"försäkring försäkringsbolag försäkrad {listed}",
                f"Vi har avtal med {listed}.",
            )
        )
    if profile.policies.referral_needed is not None:
        out.append(
            KnowledgeItem(
                "remiss",
                "remiss remisskrav",
                "Ja, du behöver en remiss för att boka tid."
                if profile.policies.referral_needed
                else "Nej, du behöver ingen remiss – du kan boka direkt.",
            )
        )

    practical = (
        ("parkering", "parkering parkera bil garage", profile.practical.parking),
        ("kollektivtrafik", "tunnelbana buss spårvagn kollektivt", profile.practical.transit),
        (
            "tillgänglighet",
            "tillgänglighet rullstol hiss trappor barnvagn",
            profile.practical.accessibility,
        ),
    )
    out.extend(KnowledgeItem(t, k, v) for t, k, v in practical if v)

    if profile.address:
        out.append(
            KnowledgeItem(
                "adress",
                "adress hitta vägbeskrivning ligger finns gatan",
                f"Vi finns på {profile.address}.",
            )
        )

    open_days = [h for h in profile.opening_hours if not h.closed and h.open and h.close]
    if open_days:
        first = open_days[0]
        out.append(
            KnowledgeItem(
                "öppettider",
                "öppettider öppet stänger stängt helg lördag söndag",
                f"Vi har öppet vardagar mellan klockan {first.open.hour} och {first.close.hour}.",
            )
        )

    out.extend(
        KnowledgeItem(
            "pris",
            f"pris kostar kostnad avgift {s.name}",
            f"{s.name} kostar {speech.speak_price(s.price_sek)}.",
        )
        for s in profile.services
        if s.price_sek is not None
    )

    return out


async def knowledge(
    session: AsyncSession, clinic: Clinic, *, question: str, now: datetime
) -> ToolResult:
    """Answer a practical question from the reviewed profile, or promise a callback."""
    verdict = safety.check(question)
    if verdict.is_blocked:
        return await _escalate_for(session, clinic, verdict, now=now, source="knowledge")

    profile = _profile(clinic)
    candidates = _knowledge_candidates(profile)
    if not candidates:
        return ToolResult(
            "Det ska jag be en kollega återkomma om.", {"answered": False, "confidence": 0.0}
        )

    # Scored on keywords only — never on the answer body. See _knowledge_candidates.
    scored = [(score_text(question, item.keywords), item) for item in candidates]
    confidence, best = max(scored, key=lambda row: row[0])

    if confidence < KNOWLEDGE_CONFIDENCE_FLOOR:
        # Spec §3: low confidence means promise a callback. Never improvise — especially
        # not a price (spec §4 rule 5).
        return ToolResult(
            "Det är jag faktiskt inte säker på – det ska jag be en kollega återkomma om.",
            {"answered": False, "confidence": round(confidence, 2), "question": question},
        )

    return ToolResult(
        best.answer,
        {
            "answered": True,
            "confidence": round(confidence, 2),
            "topic": best.topic,
            "question": question,
        },
    )


# --- escalation -------------------------------------------------------------------------------


async def _escalate_for(
    session: AsyncSession,
    clinic: Clinic,
    verdict: safety.SafetyVerdict,
    *,
    now: datetime,
    source: str,
) -> ToolResult:
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.ESCALATION,
        payload={"reason": verdict.flag.value, "matched": list(verdict.matched), "source": source},
        actor="agent",
    )
    return ToolResult(
        verdict.speech,
        {
            "escalated": True,
            "reason": verdict.flag.value,
            "matched": list(verdict.matched),
            "requires_callback": verdict.flag is safety.RedFlag.EMERGENCY,
        },
    )


async def escalate(
    session: AsyncSession,
    clinic: Clinic,
    *,
    reason: str | None = None,
    phone: str | None = None,
    name: str | None = None,
    now: datetime,
) -> ToolResult:
    """Hand off to a human. Also the tool the agent calls on any medical red flag."""
    verdict = safety.check(reason)
    if verdict.is_blocked:
        return await _escalate_for(session, clinic, verdict, now=now, source="escalate")

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.ESCALATION,
        payload={"reason": reason or "human_requested", "name": name, "phone": phone},
        actor="agent",
    )
    return ToolResult(
        safety.CALLBACK_SPEECH,
        {"escalated": True, "reason": reason or "human_requested", "requires_callback": True},
    )


__all__ = [
    "MAX_OPTIONS",
    "ToolResult",
    "add_to_waitlist",
    "availability",
    "book",
    "cancel",
    "escalate",
    "knowledge",
    "reschedule",
]
