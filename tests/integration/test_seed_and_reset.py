"""Seeding a sandbox end-to-end, and the guarantees /sim/reset must hold."""

from __future__ import annotations

import json
import uuid
from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fixtures import load_demo_profile_document
from app.models import Booking, BookingStatus, Clinic, Patient, Slot, SlotStatus, Waitlist
from app.risk import risk_band
from app.seed import SeedConfig, seed_clinic
from app.timeutils import DEFAULT_TZ, local_naive_to_utc, to_local

STHLM = DEFAULT_TZ
FOUNDER = "+46701234567"
NOW = local_naive_to_utc(date(2026, 8, 12), time(9, 0), STHLM)  # a Wednesday


async def make_clinic(
    session: AsyncSession, slug: str = "demo", *, source: str = "hardcoded"
) -> Clinic:
    doc = load_demo_profile_document()
    payload = json.loads(doc.model_dump_json())
    payload["meta"]["source"] = source
    clinic = Clinic(
        id=uuid.uuid4(),
        name=doc.profile.name,
        slug=slug,
        profile=payload,
        timezone="Europe/Stockholm",
    )
    session.add(clinic)
    await session.flush()
    return clinic


def cfg(**kw) -> SeedConfig:
    return SeedConfig(founder_phone=FOUNDER, **kw)


async def _count(session: AsyncSession, model, clinic_id: uuid.UUID) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(model.clinic_id == clinic_id)
    )


# --- seeding -----------------------------------------------------------------------------------


async def test_seed_produces_a_believable_fortnight(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    result = await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    assert result.counts["resources"] == 4
    assert result.counts["services"] == 8
    assert result.counts["slots"] > 300
    assert result.counts["waitlist"] == 9
    assert 3 <= result.counts["demo_slots"] <= 5
    assert result.fingerprint


async def test_every_slot_lands_inside_clinic_hours(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    slots = (await db_session.execute(select(Slot).where(Slot.clinic_id == clinic.id))).scalars()
    for slot in slots:
        local = to_local(slot.starts_at, STHLM)
        assert local.weekday() < 5, "no weekend slots"
        assert 7 <= local.hour <= 17


async def test_risk_panel_gets_a_real_spread_not_all_green(db_session: AsyncSession) -> None:
    """Past no-show history is what makes the step-6 risk panel worth showing."""
    clinic = await make_clinic(db_session)
    result = await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    assert result.risk_band_counts["green"] > 0
    assert result.risk_band_counts["yellow"] > 0

    noshows = await db_session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(Booking.clinic_id == clinic.id, Booking.status == BookingStatus.NOSHOW)
    )
    assert noshows > 0

    with_history = await db_session.scalar(
        select(func.count())
        .select_from(Patient)
        .where(Patient.clinic_id == clinic.id, Patient.prior_noshows > 0)
    )
    assert with_history > 0


async def test_booking_risk_snapshots_match_the_heuristic_bands(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    rows = (
        await db_session.execute(
            select(Booking).where(
                Booking.clinic_id == clinic.id, Booking.status == BookingStatus.CONFIRMED
            )
        )
    ).scalars()
    for b in rows:
        assert b.risk is not None
        assert 0 <= float(b.risk) <= 0.85
        assert risk_band(float(b.risk)) in {"green", "yellow", "red"}


async def test_demo_slots_are_actually_open(db_session: AsyncSession) -> None:
    """The whole point of force-opening them: the demo call must find them free."""
    clinic = await make_clinic(db_session)
    result = await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    rows = (
        (await db_session.execute(select(Slot).where(Slot.id.in_(result.demo_slot_ids))))
        .scalars()
        .all()
    )
    assert len(rows) == len(result.demo_slot_ids)
    assert all(s.status is SlotStatus.OPEN for s in rows)
    assert all(s.starts_at >= NOW + timedelta(minutes=90) for s in rows)


async def test_exactly_one_patient_has_the_founder_mobile(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    result = await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    n = await db_session.scalar(
        select(func.count())
        .select_from(Patient)
        .where(Patient.clinic_id == clinic.id, Patient.phone == FOUNDER)
    )
    assert n == 1
    assert result.founder_waitlist_id is not None


async def test_every_seeded_patient_is_flagged_as_demo(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    real = await db_session.scalar(
        select(func.count())
        .select_from(Patient)
        .where(Patient.clinic_id == clinic.id, Patient.is_demo.is_(False))
    )
    assert real == 0


async def test_booked_slots_and_confirmed_bookings_agree(db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    booked_slots = await db_session.scalar(
        select(func.count())
        .select_from(Slot)
        .where(Slot.clinic_id == clinic.id, Slot.status == SlotStatus.BOOKED)
    )
    bookings = await _count(db_session, Booking, clinic.id)
    assert booked_slots == bookings


async def test_booking_refs_are_unique_and_voice_safe(db_session: AsyncSession) -> None:
    from app.booking_ref import ALPHABET

    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    refs = (
        (
            await db_session.execute(
                select(Booking.booking_ref).where(Booking.clinic_id == clinic.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(refs) == len(set(refs))
    for ref in refs:
        assert len(ref) == 6
        assert all(ch in ALPHABET for ch in ref), f"{ref} contains a character STT will mangle"


async def test_waitlist_windows_cover_the_demo_slots(db_session: AsyncSession) -> None:
    """If the windows miss, the refill cascade finds no candidates and the demo dies."""
    clinic = await make_clinic(db_session)
    result = await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    demo = (
        (await db_session.execute(select(Slot).where(Slot.id.in_(result.demo_slot_ids))))
        .scalars()
        .all()
    )
    entries = (
        (await db_session.execute(select(Waitlist).where(Waitlist.clinic_id == clinic.id)))
        .scalars()
        .all()
    )

    for slot in demo:
        covering = [w for w in entries if w.earliest <= slot.starts_at <= w.latest]
        assert covering, f"no waitlist entry covers demo slot at {slot.starts_at}"


# --- determinism and isolation -------------------------------------------------------------------


@pytest.mark.db_commit
async def test_seeding_twice_produces_the_same_fingerprint(
    committing_db_session: AsyncSession,
) -> None:
    session = committing_db_session
    from sqlalchemy import delete

    from app.models import CLINIC_SCOPED_DELETE_ORDER

    clinic = await make_clinic(session)
    first = await seed_clinic(session, clinic, now=NOW, cfg=cfg())
    await session.commit()

    for model in CLINIC_SCOPED_DELETE_ORDER:
        await session.execute(delete(model).where(model.clinic_id == clinic.id))
    second = await seed_clinic(session, clinic, now=NOW, cfg=cfg())
    await session.commit()

    assert first.fingerprint == second.fingerprint
    assert first.counts == second.counts


async def test_two_clinics_seeded_the_same_day_differ(db_session: AsyncSession) -> None:
    """Decision D5: two demo meetings on one day must not show identical data."""
    a = await make_clinic(db_session, slug="clinic-a")
    b = await make_clinic(db_session, slug="clinic-b")

    ra = await seed_clinic(db_session, a, now=NOW, cfg=cfg())
    rb = await seed_clinic(db_session, b, now=NOW, cfg=cfg())

    assert ra.fingerprint != rb.fingerprint


async def test_seeding_one_clinic_does_not_touch_another(db_session: AsyncSession) -> None:
    a = await make_clinic(db_session, slug="clinic-a")
    b = await make_clinic(db_session, slug="clinic-b")
    await seed_clinic(db_session, a, now=NOW, cfg=cfg())

    before = await _count(db_session, Slot, a.id)
    await seed_clinic(db_session, b, now=NOW, cfg=cfg())
    assert await _count(db_session, Slot, a.id) == before


async def test_seed_emits_one_event_not_hundreds(db_session: AsyncSession) -> None:
    """A booking_created per seeded booking would bury whatever the demo is showing."""
    from app.events import recent_events

    clinic = await make_clinic(db_session)
    await seed_clinic(db_session, clinic, now=NOW, cfg=cfg())

    events = await recent_events(db_session, clinic.id)
    assert len(events) == 1
    assert events[0].type == "seed_completed"
    assert events[0].actor == "seed"
