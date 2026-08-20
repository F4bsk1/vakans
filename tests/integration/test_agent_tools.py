"""The agent tools against a real seeded sandbox."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools
from app.config import get_settings
from app.events import recent_events
from app.models import (
    Booking,
    BookingStatus,
    Patient,
    Service,
    Slot,
    SlotStatus,
    SmsMessage,
    Waitlist,
)
from app.seed import SeedConfig, seed_clinic
from tests.integration.test_seed_and_reset import NOW, make_clinic

CALLER = "070-555 12 34"


@pytest.fixture
def agent_settings():
    """Named to avoid shadowing conftest's session-scoped `settings`."""
    return get_settings().model_copy(
        update={"sms_provider": "fake", "founder_mobile": "+46701234567"}
    )


async def seeded(session: AsyncSession, slug: str = "demo"):
    clinic = await make_clinic(session, slug)
    result = await seed_clinic(
        session, clinic, now=NOW, cfg=SeedConfig(founder_phone="+46701234567")
    )
    return clinic, result


async def an_open_slot(session: AsyncSession, clinic, result):
    slot = (
        await session.execute(select(Slot).where(Slot.id == result.demo_slot_ids[0]))
    ).scalar_one()
    service = (
        (
            await session.execute(
                select(Service).where(Service.clinic_id == clinic.id).order_by(Service.name)
            )
        )
        .scalars()
        .first()
    )
    return slot, service


# --- availability ------------------------------------------------------------------------------


async def test_availability_speaks_swedish_times(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.availability(db_session, clinic, now=NOW)

    assert result.data["slots"]
    assert len(result.data["slots"]) <= tools.MAX_OPTIONS
    assert "kl" in result.speech
    assert "hos" in result.speech


async def test_availability_offers_distinct_times(db_session: AsyncSession) -> None:
    """Four practitioners free at 08:00 is one option to offer a caller, not four."""
    clinic, _ = await seeded(db_session)
    result = await tools.availability(db_session, clinic, now=NOW)
    starts = [s["starts_at"] for s in result.data["slots"]]
    assert len(set(starts)) == len(starts)


async def test_availability_matches_a_service_the_caller_half_said(
    db_session: AsyncSession,
) -> None:
    """STT gives "fysio", not "Nybesök fysioterapi"."""
    clinic, _ = await seeded(db_session)
    result = await tools.availability(db_session, clinic, service_name="fysio", now=NOW)
    assert result.data["service_name"] is not None
    assert "fysio" in result.data["service_name"].lower()


async def test_availability_asks_again_when_the_service_is_unrecognisable(
    db_session: AsyncSession,
) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.availability(db_session, clinic, service_name="bilservice", now=NOW)
    assert result.data["matched"] is False
    assert "inte säker" in result.speech


async def test_availability_never_offers_a_slot_within_90_minutes(
    db_session: AsyncSession,
) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.availability(db_session, clinic, now=NOW)
    for slot in result.data["slots"]:
        assert slot["starts_at"] >= (NOW + timedelta(minutes=90)).isoformat()


async def test_availability_escalates_on_a_red_flag(db_session: AsyncSession) -> None:
    """The safety layer runs before any booking logic, whichever field the words arrive in."""
    clinic, _ = await seeded(db_session)
    result = await tools.availability(
        db_session, clinic, service_name="jag har ont i bröstet", now=NOW
    )
    assert result.data["escalated"] is True
    assert "112" in result.speech


# --- book --------------------------------------------------------------------------------------


async def test_book_claims_the_slot_and_confirms_in_swedish(
    db_session: AsyncSession, agent_settings
) -> None:
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)

    booked = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna Andersson",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    assert booked.data["booked"] is True
    assert "bokat" in booked.speech
    assert booked.data["booking_ref"]
    # The reference is spelled out so TTS dictates it rather than reading it as a word.
    assert " ".join(booked.data["booking_ref"]) in booked.speech

    await db_session.refresh(slot)
    assert slot.status is SlotStatus.BOOKED


async def test_book_sends_a_confirmation_sms(db_session: AsyncSession, agent_settings) -> None:
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)

    await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    sms = (
        (await db_session.execute(select(SmsMessage).where(SmsMessage.to_phone == "+46705551234")))
        .scalars()
        .all()
    )
    assert len(sms) == 1
    assert "Ref" in sms[0].body


async def test_booking_the_same_slot_twice_is_refused(
    db_session: AsyncSession, agent_settings
) -> None:
    """The demo deliberately races a voice call against an inbound refill "JA"."""
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)

    first = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    second = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Bertil",
        phone="070-555 99 88",
        settings=agent_settings,
        now=NOW,
    )

    assert first.data["booked"] is True
    assert second.data["booked"] is False
    assert second.data["reason"] == "slot_taken"
    assert "hann tyvärr bli bokad" in second.speech


async def test_repeat_caller_is_not_duplicated(db_session: AsyncSession, agent_settings) -> None:
    """Delta G5: one phone, one patient — otherwise the "ny patient" risk term is meaningless."""
    clinic, result = await seeded(db_session)
    slots = (
        (await db_session.execute(select(Slot).where(Slot.id.in_(result.demo_slot_ids))))
        .scalars()
        .all()
    )
    service = (
        (await db_session.execute(select(Service).where(Service.clinic_id == clinic.id)))
        .scalars()
        .first()
    )

    first = await tools.book(
        db_session,
        clinic,
        slot_id=slots[0].id,
        service_id=service.id,
        patient_name="Anna Andersson",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    second = await tools.book(
        db_session,
        clinic,
        slot_id=slots[1].id,
        service_id=service.id,
        patient_name="Anna Andersson",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    count = await db_session.scalar(
        select(func.count()).select_from(Patient).where(Patient.phone == "+46705551234")
    )
    assert count == 1
    # First visit scores as a new patient; the second does not.
    assert first.data["risk"] > second.data["risk"]


async def test_book_emits_a_booking_created_event_with_the_price(
    db_session: AsyncSession, agent_settings
) -> None:
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)
    await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    events = [e for e in await recent_events(db_session, clinic.id) if e.type == "booking_created"]
    assert len(events) == 1
    assert events[0].actor == "agent"
    assert events[0].sek_value == service.price_sek


async def test_book_rejects_an_unknown_slot(db_session: AsyncSession, agent_settings) -> None:
    clinic, result = await seeded(db_session)
    _, service = await an_open_slot(db_session, clinic, result)
    booked = await tools.book(
        db_session,
        clinic,
        slot_id=uuid.uuid4(),
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    assert booked.data["booked"] is False


# --- cancel and reschedule ----------------------------------------------------------------------


async def test_cancel_frees_the_slot_and_signals_the_refill_engine(
    db_session: AsyncSession, agent_settings
) -> None:
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)
    booked = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    cancelled = await tools.cancel(
        db_session, clinic, booking_ref=booked.data["booking_ref"], phone=CALLER, now=NOW
    )
    assert cancelled.data["cancelled"] is True
    assert "avbokad" in cancelled.speech

    await db_session.refresh(slot)
    assert slot.status is SlotStatus.OPEN

    types = [e.type for e in await recent_events(db_session, clinic.id)]
    assert "booking_cancelled" in types
    # Step 3b's cascade hangs off this one.
    assert "slot_freed" in types


async def test_cancel_refuses_when_the_phone_does_not_match_the_reference(
    db_session: AsyncSession, agent_settings
) -> None:
    """Spec §4 rule 4: a reference alone must never be enough to touch a booking."""
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)
    booked = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    cancelled = await tools.cancel(
        db_session,
        clinic,
        booking_ref=booked.data["booking_ref"],
        phone="070-000 00 00",
        now=NOW,
    )
    assert cancelled.data["cancelled"] is False
    await db_session.refresh(slot)
    assert slot.status is SlotStatus.BOOKED


async def test_cancel_finds_a_booking_by_phone_alone(
    db_session: AsyncSession, agent_settings
) -> None:
    """Callers rarely have the SMS in front of them."""
    clinic, result = await seeded(db_session)
    slot, service = await an_open_slot(db_session, clinic, result)
    await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Anna Andersson",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    cancelled = await tools.cancel(db_session, clinic, phone=CALLER, name="Anna Andersson", now=NOW)
    assert cancelled.data["cancelled"] is True


async def test_cancel_reports_not_found_rather_than_guessing(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    cancelled = await tools.cancel(db_session, clinic, phone="070-111 22 33", now=NOW)
    assert cancelled.data["cancelled"] is False
    assert "hittar tyvärr ingen bokning" in cancelled.speech


async def test_reschedule_moves_the_booking_and_frees_the_old_slot(
    db_session: AsyncSession, agent_settings
) -> None:
    clinic, result = await seeded(db_session)
    slots = (
        (await db_session.execute(select(Slot).where(Slot.id.in_(result.demo_slot_ids))))
        .scalars()
        .all()
    )
    service = (
        (await db_session.execute(select(Service).where(Service.clinic_id == clinic.id)))
        .scalars()
        .first()
    )

    booked = await tools.book(
        db_session,
        clinic,
        slot_id=slots[0].id,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )
    moved = await tools.reschedule(
        db_session,
        clinic,
        new_slot_id=slots[1].id,
        booking_ref=booked.data["booking_ref"],
        phone=CALLER,
        settings=agent_settings,
        now=NOW,
    )

    assert moved.data["rescheduled"] is True
    assert "flyttat" in moved.speech

    await db_session.refresh(slots[0])
    await db_session.refresh(slots[1])
    assert slots[0].status is SlotStatus.OPEN
    assert slots[1].status is SlotStatus.BOOKED

    booking = (
        await db_session.execute(
            select(Booking).where(Booking.booking_ref == booked.data["booking_ref"])
        )
    ).scalar_one()
    assert booking.slot_id == slots[1].id
    assert booking.status is BookingStatus.CONFIRMED


# --- knowledge ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("behöver jag remiss?", "remiss"),
        ("hur avbokar jag?", "24 timmar"),
        ("kan jag parkera nånstans?", "parkering"),
        ("var ligger ni?", "Sankt Eriksgatan"),
    ],
)
async def test_knowledge_answers_practical_questions(
    db_session: AsyncSession, question: str, expected: str
) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.knowledge(db_session, clinic, question=question, now=NOW)
    assert result.data["answered"] is True, result.data
    assert expected.lower() in result.speech.lower()


async def test_knowledge_promises_a_callback_when_unsure(db_session: AsyncSession) -> None:
    """Spec §3: low confidence means promise a callback, never improvise."""
    clinic, _ = await seeded(db_session)
    result = await tools.knowledge(
        db_session, clinic, question="har ni en simbassäng och astronaututbildning?", now=NOW
    )
    assert result.data["answered"] is False
    assert "återkomma" in result.speech


async def test_knowledge_declines_symptom_questions(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.knowledge(
        db_session, clinic, question="jag har ont i knät, vad tror du det är?", now=NOW
    )
    assert result.data.get("escalated") is True
    assert "1177" in result.speech


async def test_knowledge_escalates_an_emergency_immediately(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.knowledge(
        db_session, clinic, question="min pappa har bröstsmärtor och andnöd", now=NOW
    )
    assert "112" in result.speech
    types = [e.type for e in await recent_events(db_session, clinic.id)]
    assert "escalation" in types


# --- waitlist and escalation ---------------------------------------------------------------------


async def test_waitlist_adds_an_entry(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    service = (
        (await db_session.execute(select(Service).where(Service.clinic_id == clinic.id)))
        .scalars()
        .first()
    )

    result = await tools.add_to_waitlist(
        db_session,
        clinic,
        service_id=service.id,
        patient_name="Anna",
        phone=CALLER,
        now=NOW,
    )
    assert result.data["waitlisted"] is True
    entry = (
        await db_session.execute(
            select(Waitlist).where(Waitlist.id == uuid.UUID(result.data["waitlist_id"]))
        )
    ).scalar_one()
    assert entry.service_id == service.id


async def test_escalate_records_a_human_handoff(db_session: AsyncSession) -> None:
    clinic, _ = await seeded(db_session)
    result = await tools.escalate(
        db_session, clinic, reason="vill prata med en människa", phone=CALLER, now=NOW
    )
    assert result.data["escalated"] is True
    assert result.data["requires_callback"] is True
    types = [e.type for e in await recent_events(db_session, clinic.id)]
    assert "escalation" in types
