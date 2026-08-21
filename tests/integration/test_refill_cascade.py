"""The refill cascade against a real seeded sandbox.

This file is the checkpoint for the second half of step 3: cancel a booking, watch the SMS go
out, let the window close, watch it advance, answer JA, and see the slot refilled with SEK
attached. If these pass, the demo's central beat works.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools
from app.config import get_settings
from app.events import recent_events
from app.models import (
    Booking,
    BookingStatus,
    Patient,
    RefillOffer,
    RefillOfferStatus,
    Service,
    Slot,
    SlotStatus,
    SmsDirection,
    SmsMessage,
    Waitlist,
    WaitlistStatus,
)
from app.refill import engine, worker
from app.seed import SeedConfig, seed_clinic
from tests.integration.test_seed_and_reset import NOW, make_clinic

FOUNDER = "+46701234567"


@pytest.fixture
def refill_settings():
    return get_settings().model_copy(
        update={"sms_provider": "fake", "founder_mobile": FOUNDER, "refill_offer_timeout_s": 90}
    )


async def armed(session: AsyncSession, slug: str = "demo"):
    clinic = await make_clinic(session, slug)
    result = await seed_clinic(session, clinic, now=NOW, cfg=SeedConfig(founder_phone=FOUNDER))
    return clinic, result


async def a_booked_future_slot(session: AsyncSession, clinic, *, with_candidates: bool = True):
    """A confirmed booking whose slot a cascade can actually fill."""
    rows = (
        await session.execute(
            select(Booking, Slot, Service)
            .join(Slot, Booking.slot_id == Slot.id)
            .join(Service, Booking.service_id == Service.id)
            .where(
                Booking.clinic_id == clinic.id,
                Booking.status == BookingStatus.CONFIRMED,
                Slot.starts_at > NOW,
            )
            .order_by(Slot.starts_at)
        )
    ).all()
    assert rows, "seed produced no upcoming bookings"

    if not with_candidates:
        return rows[0]
    for booking, slot, service in rows:
        if await engine.find_candidates(session, clinic, slot, service_id=service.id, now=NOW):
            return booking, slot, service
    pytest.skip("seed produced no bookable slot with waitlist candidates")


async def free_it(session, clinic, booking, slot, service, settings, *, now=NOW):
    booking.status = BookingStatus.CANCELLED
    slot.status = SlotStatus.OPEN
    await session.flush()
    return await engine.free_slot_and_cascade(
        session, clinic, slot, service, settings=settings, now=now
    )


# --- starting a cascade ---------------------------------------------------------------------------


async def test_a_freed_slot_texts_the_best_candidate_and_holds_the_slot(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)

    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)

    assert state.outcome == "offered"
    assert state.candidate_n == 1
    assert state.expires_at == NOW + timedelta(seconds=90)

    await db_session.refresh(slot)
    # The whole reason schema delta G6 exists: nobody may book this out from under the person
    # who was just promised ninety seconds.
    assert slot.status is SlotStatus.HELD

    sms = (
        (
            await db_session.execute(
                select(SmsMessage).where(SmsMessage.refill_offer_id == state.offer_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(sms) == 1
    assert "Svara JA" in sms[0].body

    types = [e.type for e in await recent_events(db_session, clinic.id)]
    assert "slot_freed" in types
    assert "refill_sms_sent" in types


async def test_the_voice_agent_cannot_book_a_slot_a_cascade_is_holding(
    db_session: AsyncSession, refill_settings
) -> None:
    """The race the demo deliberately creates, resolved in the waiting patient's favour."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    await free_it(db_session, clinic, booking, slot, service, refill_settings)

    attempt = await tools.book(
        db_session,
        clinic,
        slot_id=slot.id,
        service_id=service.id,
        patient_name="Snabb Ringare",
        phone="070-999 88 77",
        settings=refill_settings,
        now=NOW,
    )

    assert attempt.data["booked"] is False
    assert attempt.data["reason"] == "slot_taken"


async def test_availability_never_offers_a_held_slot(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    await free_it(db_session, clinic, booking, slot, service, refill_settings)

    offered = await tools.availability(db_session, clinic, now=NOW)

    assert str(slot.id) not in [s["slot_id"] for s in offered.data["slots"]]


async def test_a_slot_in_the_past_starts_no_cascade(
    db_session: AsyncSession, refill_settings
) -> None:
    """Texting nine people about a slot that has already been and gone is worse than silence."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)

    state = await free_it(
        db_session,
        clinic,
        booking,
        slot,
        service,
        refill_settings,
        now=slot.starts_at + timedelta(hours=1),
    )

    assert state.outcome == "slot_unavailable"
    assert state.reason == "slot_in_past"


# --- the timeout ------------------------------------------------------------------------------------


async def test_the_window_closing_advances_to_the_next_candidate(
    db_session: AsyncSession, refill_settings
) -> None:
    """Spec §3 step 4: "Timeout 90 s → nästa kandidat"."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    first = await free_it(db_session, clinic, booking, slot, service, refill_settings)

    advanced = await engine.sweep_expired(
        db_session, settings=refill_settings, now=NOW + timedelta(seconds=91)
    )

    assert len(advanced) == 1
    offers = (
        (
            await db_session.execute(
                select(RefillOffer)
                .where(RefillOffer.cascade_id == first.cascade_id)
                .order_by(RefillOffer.candidate_n)
            )
        )
        .scalars()
        .all()
    )
    assert [o.candidate_n for o in offers] == [1, 2]
    assert offers[0].status is RefillOfferStatus.EXPIRED
    assert offers[1].status is RefillOfferStatus.SENT
    # Two different people. Re-texting the same person is the classic cascade bug.
    assert offers[0].patient_id != offers[1].patient_id

    await db_session.refresh(slot)
    assert slot.status is SlotStatus.HELD

    types = [e.type for e in await recent_events(db_session, clinic.id)]
    assert "refill_offer_expired" in types


async def test_a_cascade_does_not_advance_before_its_window_closes(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    await free_it(db_session, clinic, booking, slot, service, refill_settings)

    advanced = await engine.sweep_expired(
        db_session, settings=refill_settings, now=NOW + timedelta(seconds=89)
    )

    assert advanced == []


async def test_an_exhausted_cascade_releases_the_slot_and_says_so(
    db_session: AsyncSession, refill_settings
) -> None:
    """The slot must not stay `held` forever with nobody left to offer it to."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)

    now = NOW
    for _ in range(20):
        now += timedelta(seconds=91)
        if not await engine.sweep_expired(db_session, settings=refill_settings, now=now):
            break
        await db_session.refresh(slot)
        if slot.status is SlotStatus.OPEN:
            break

    await db_session.refresh(slot)
    assert slot.status is SlotStatus.OPEN

    types = [e.type for e in await recent_events(db_session, clinic.id, limit=500)]
    assert "refill_cascade_ended" in types
    assert state.cascade_id is not None


# --- the JA ---------------------------------------------------------------------------------------


async def test_ja_refills_the_slot_and_books_the_revenue(
    db_session: AsyncSession, refill_settings
) -> None:
    """The money shot: cancel -> SMS -> JA -> booked, with SEK on the event."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)

    patient = await db_session.get(Patient, (await _offer(db_session, state)).patient_id)
    result = await engine.record_reply(
        db_session,
        clinic_id=clinic.id,
        from_phone=patient.phone,
        body="JA tack",
        settings=refill_settings,
        now=NOW + timedelta(seconds=30),
    )

    assert result.accepted is True
    assert result.booking_ref

    await db_session.refresh(slot)
    assert slot.status is SlotStatus.BOOKED

    refilled = (
        (
            await db_session.execute(
                select(Booking).where(
                    Booking.slot_id == slot.id, Booking.status == BookingStatus.CONFIRMED
                )
            )
        )
        .scalars()
        .one()
    )
    assert refilled.booked_via == "refill"
    assert refilled.patient_id == patient.id

    offer = await _offer(db_session, state)
    assert offer.status is RefillOfferStatus.ACCEPTED
    entry = await db_session.get(Waitlist, offer.waitlist_id)
    assert entry.status is WaitlistStatus.FULFILLED

    events = await recent_events(db_session, clinic.id, limit=500)
    refills = [e for e in events if e.type == "slot_refilled"]
    assert len(refills) == 1
    # The money is the price of what the *waitlist* patient asked for, not of the appointment
    # that was cancelled — they get the treatment they were waiting for, in the freed hole.
    booked_service = await db_session.get(Service, refilled.service_id)
    assert refills[0].sek_value == booked_service.price_sek
    # And exactly one event carries it: `booking_created` has no sek_value, so the ROI counter
    # cannot double-count the same recovered revenue.
    assert [e for e in events if e.type == "booking_created" and e.sek_value is not None] == []

    confirmation = (
        (
            await db_session.execute(
                select(SmsMessage).where(
                    SmsMessage.patient_id == patient.id, SmsMessage.direction == SmsDirection.OUT
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(result.booking_ref in m.body for m in confirmation)


async def test_a_no_advances_immediately_rather_than_waiting_out_the_clock(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)
    patient = await db_session.get(Patient, (await _offer(db_session, state)).patient_id)

    result = await engine.record_reply(
        db_session,
        clinic_id=clinic.id,
        from_phone=patient.phone,
        body="Nej tack",
        settings=refill_settings,
        now=NOW + timedelta(seconds=10),
    )

    assert result.accepted is False
    assert result.reason == "declined"

    offers = (
        (
            await db_session.execute(
                select(RefillOffer)
                .where(RefillOffer.cascade_id == state.cascade_id)
                .order_by(RefillOffer.candidate_n)
            )
        )
        .scalars()
        .all()
    )
    assert offers[0].status is RefillOfferStatus.DECLINED
    assert len(offers) == 2  # candidate 2 was texted ten seconds in, not ninety


async def test_a_late_ja_still_wins_if_nobody_took_the_slot(
    db_session: AsyncSession, refill_settings
) -> None:
    """A patient whose thumb was two seconds slow should not lose a slot that is still empty."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)
    first = await _offer(db_session, state)
    patient = await db_session.get(Patient, first.patient_id)

    await engine.sweep_expired(
        db_session, settings=refill_settings, now=NOW + timedelta(seconds=91)
    )

    result = await engine.record_reply(
        db_session,
        clinic_id=clinic.id,
        from_phone=patient.phone,
        body="JA",
        settings=refill_settings,
        now=NOW + timedelta(seconds=95),
    )

    assert result.accepted is True
    # Whoever was mid-offer when the yes landed is superseded, not left dangling.
    others = (
        (
            await db_session.execute(
                select(RefillOffer).where(
                    RefillOffer.cascade_id == state.cascade_id,
                    RefillOffer.candidate_n > first.candidate_n,
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(o.status is RefillOfferStatus.SUPERSEDED for o in others)


async def test_an_unclear_reply_asks_again_instead_of_guessing(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)
    patient = await db_session.get(Patient, (await _offer(db_session, state)).patient_id)

    result = await engine.record_reply(
        db_session,
        clinic_id=clinic.id,
        from_phone=patient.phone,
        body="Vad kostar det?",
        settings=refill_settings,
        now=NOW + timedelta(seconds=20),
    )

    assert result.accepted is False
    assert result.reason == "unclear"
    await db_session.refresh(slot)
    assert slot.status is SlotStatus.HELD  # still theirs, the clock is still running


async def test_a_redelivered_webhook_does_not_book_twice(
    db_session: AsyncSession, refill_settings
) -> None:
    """46elks retries anything it did not get a 2xx for. Booking the same slot twice off a
    redelivery would be a memorable way to lose a clinic."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)
    patient = await db_session.get(Patient, (await _offer(db_session, state)).patient_id)

    kwargs = dict(
        clinic_id=clinic.id,
        from_phone=patient.phone,
        body="JA",
        settings=refill_settings,
        now=NOW + timedelta(seconds=30),
        provider="46elks",
        provider_message_id="s0meElksId",
    )
    first = await engine.record_reply(db_session, **kwargs)
    second = await engine.record_reply(db_session, **kwargs)

    assert first.accepted is True
    assert second.duplicate is True
    assert second.accepted is False

    confirmed = (
        (
            await db_session.execute(
                select(Booking).where(
                    Booking.slot_id == slot.id, Booking.status == BookingStatus.CONFIRMED
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(confirmed) == 1


async def test_a_reply_from_a_stranger_is_recorded_and_ignored(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)

    result = await engine.record_reply(
        db_session,
        clinic_id=clinic.id,
        from_phone="+46700000999",
        body="JA",
        settings=refill_settings,
        now=NOW,
    )

    assert result.accepted is False
    assert result.reason == "no_open_offer"
    inbound = (
        (
            await db_session.execute(
                select(SmsMessage).where(SmsMessage.direction == SmsDirection.IN)
            )
        )
        .scalars()
        .all()
    )
    assert len(inbound) == 1


# --- the worker ------------------------------------------------------------------------------------


async def test_the_worker_tick_delivers_the_outbox_and_advances_the_clock(
    db_session: AsyncSession, refill_settings
) -> None:
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)
    await free_it(db_session, clinic, booking, slot, service, refill_settings)

    first = await worker.tick(db_session, settings=refill_settings, now=NOW)
    assert first.sms_sent >= 1
    assert first.offers_expired == 0

    second = await worker.tick(
        db_session, settings=refill_settings, now=NOW + timedelta(seconds=91)
    )
    assert second.offers_expired == 1
    # The follow-up offer goes out in the same tick, not a poll interval later.
    assert second.sms_sent >= 1

    still_queued = (
        (
            await db_session.execute(
                select(SmsMessage).where(SmsMessage.status == "queued")  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    assert still_queued == []


async def _offer(session: AsyncSession, state) -> RefillOffer:
    return (
        (
            await session.execute(
                select(RefillOffer)
                .where(RefillOffer.cascade_id == state.cascade_id)
                .order_by(RefillOffer.candidate_n)
            )
        )
        .scalars()
        .first()
    )


async def test_the_slot_is_not_offered_back_to_whoever_just_cancelled_it(
    db_session: AsyncSession, refill_settings
) -> None:
    """Found by running the demo: correct by every rule in the spec, absurd to watch."""
    clinic, _ = await armed(db_session)
    booking, slot, service = await a_booked_future_slot(db_session, clinic)

    # Put the cancelling patient at the very front of the queue: waited longest, zero risk.
    entry = (
        (
            await db_session.execute(
                select(Waitlist).where(
                    Waitlist.clinic_id == clinic.id, Waitlist.service_id == service.id
                )
            )
        )
        .scalars()
        .first()
    )
    if entry is None:
        pytest.skip("no waitlist entry for the cancelled service")
    entry.patient_id = booking.patient_id
    entry.created_at = NOW - timedelta(days=365)
    entry.earliest = None
    entry.latest = None
    entry.short_notice_ok = True
    await db_session.flush()

    state = await free_it(db_session, clinic, booking, slot, service, refill_settings)

    if state.outcome == "offered":
        offer = await _offer(db_session, state)
        assert offer.patient_id != booking.patient_id
    else:
        assert state.outcome == "no_candidates"
