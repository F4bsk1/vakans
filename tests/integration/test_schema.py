"""The canonical schema: migration fidelity and the guarantees the deltas buy us."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.fixtures import load_demo_profile_document
from app.models import (
    Base,
    Booking,
    BookingStatus,
    Clinic,
    Patient,
    Resource,
    Service,
    Slot,
    SlotStatus,
    alembic_include_object,
)


async def test_migrations_match_models(db_connection: AsyncConnection) -> None:
    """The highest value-per-line test in the repo: catches model/migration drift every run.

    If this fails, someone changed models.py without generating a migration (or hand-edited a
    migration). Fix with `make revision m="..."`, never by editing this test.
    """

    def _diff(sync_conn: object) -> list[object]:
        ctx = MigrationContext.configure(
            sync_conn,  # type: ignore[arg-type]
            opts={
                "compare_type": True,
                "compare_server_default": True,
                # Must mirror backend/alembic/env.py, or this test and autogenerate disagree.
                "include_object": alembic_include_object,
            },
        )
        return compare_metadata(ctx, Base.metadata)

    diff = await db_connection.run_sync(_diff)
    assert diff == [], f"models.py and the migrations have drifted: {diff}"


async def _make_clinic(session: AsyncSession, slug: str = "demo") -> Clinic:
    doc = load_demo_profile_document()
    clinic = Clinic(
        id=uuid.uuid4(),
        name=doc.profile.name,
        slug=slug,
        profile=doc.model_dump(mode="json"),
        timezone="Europe/Stockholm",
    )
    session.add(clinic)
    await session.flush()
    return clinic


async def _minimal_booking_fixtures(
    session: AsyncSession, clinic: Clinic
) -> tuple[Slot, Service, Patient]:
    resource = Resource(id=uuid.uuid4(), clinic_id=clinic.id, name="Johanna Lindqvist")
    service = Service(id=uuid.uuid4(), clinic_id=clinic.id, name="Nybesök", duration_min=45)
    patient = Patient(id=uuid.uuid4(), clinic_id=clinic.id, name="Test Testsson")
    session.add_all([resource, service, patient])
    await session.flush()

    start = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    slot = Slot(
        id=uuid.uuid4(),
        clinic_id=clinic.id,
        resource_id=resource.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=45),
    )
    session.add(slot)
    await session.flush()
    return slot, service, patient


async def test_double_booking_is_rejected(db_session: AsyncSession) -> None:
    """DELTA G1. The demo deliberately races a voice call against an inbound refill "JA"."""
    clinic = await _make_clinic(db_session)
    slot, service, patient = await _minimal_booking_fixtures(db_session, clinic)

    common = {
        "clinic_id": clinic.id,
        "slot_id": slot.id,
        "service_id": service.id,
        "patient_id": patient.id,
        "status": BookingStatus.CONFIRMED,
    }
    await db_session.execute(
        insert(Booking), [{"id": uuid.uuid4(), "booking_ref": "AAA111", **common}]
    )

    with pytest.raises(IntegrityError):
        await db_session.execute(
            insert(Booking), [{"id": uuid.uuid4(), "booking_ref": "BBB222", **common}]
        )


async def test_cancelled_booking_frees_the_slot_again(db_session: AsyncSession) -> None:
    """The G1 index is partial on purpose: a cancellation must not block the refill."""
    clinic = await _make_clinic(db_session)
    slot, service, patient = await _minimal_booking_fixtures(db_session, clinic)

    common = {
        "clinic_id": clinic.id,
        "slot_id": slot.id,
        "service_id": service.id,
        "patient_id": patient.id,
    }
    await db_session.execute(
        insert(Booking),
        [
            {
                "id": uuid.uuid4(),
                "booking_ref": "AAA111",
                "status": BookingStatus.CANCELLED,
                **common,
            }
        ],
    )
    # A second, confirmed booking on the same slot is now legal.
    await db_session.execute(
        insert(Booking),
        [
            {
                "id": uuid.uuid4(),
                "booking_ref": "BBB222",
                "status": BookingStatus.CONFIRMED,
                **common,
            }
        ],
    )
    count = await db_session.scalar(
        select(text("count(*)")).select_from(Booking).where(Booking.slot_id == slot.id)
    )
    assert count == 2


async def test_duplicate_patient_phone_is_rejected(db_session: AsyncSession) -> None:
    """DELTA G5: without this, /agent/book mints a new patient on every call from one number,
    which destroys the "ny patient" risk signal."""
    clinic = await _make_clinic(db_session)
    db_session.add(Patient(id=uuid.uuid4(), clinic_id=clinic.id, phone="+46701112233"))
    await db_session.flush()

    db_session.add(Patient(id=uuid.uuid4(), clinic_id=clinic.id, phone="+46701112233"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_patients_without_phone_are_not_deduplicated(db_session: AsyncSession) -> None:
    """The G5 unique index is partial: many walk-ins may have no number at all."""
    clinic = await _make_clinic(db_session)
    db_session.add_all(
        [
            Patient(id=uuid.uuid4(), clinic_id=clinic.id, name="A", phone=None),
            Patient(id=uuid.uuid4(), clinic_id=clinic.id, name="B", phone=None),
        ]
    )
    await db_session.flush()  # must not raise


async def test_same_phone_in_two_clinics_is_allowed(db_session: AsyncSession) -> None:
    """Decision D5: clinics are independent sandboxes that must never collide."""
    a = await _make_clinic(db_session, slug="clinic-a")
    b = await _make_clinic(db_session, slug="clinic-b")
    db_session.add_all(
        [
            Patient(id=uuid.uuid4(), clinic_id=a.id, phone="+46701112233"),
            Patient(id=uuid.uuid4(), clinic_id=b.id, phone="+46701112233"),
        ]
    )
    await db_session.flush()  # must not raise


async def test_slot_must_end_after_it_starts(db_session: AsyncSession) -> None:
    clinic = await _make_clinic(db_session)
    resource = Resource(id=uuid.uuid4(), clinic_id=clinic.id, name="Johanna")
    db_session.add(resource)
    await db_session.flush()

    start = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    db_session.add(
        Slot(
            id=uuid.uuid4(),
            clinic_id=clinic.id,
            resource_id=resource.id,
            starts_at=start,
            ends_at=start - timedelta(minutes=15),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_slot_status_accepts_held(db_session: AsyncSession) -> None:
    """DELTA G6: 'held' must exist from the baseline, since changing a CHECK later is a real
    migration and the refill window needs it in step 3."""
    clinic = await _make_clinic(db_session)
    resource = Resource(id=uuid.uuid4(), clinic_id=clinic.id, name="Johanna")
    db_session.add(resource)
    await db_session.flush()

    start = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
    db_session.add(
        Slot(
            id=uuid.uuid4(),
            clinic_id=clinic.id,
            resource_id=resource.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=SlotStatus.HELD,
        )
    )
    await db_session.flush()  # must not raise


async def test_deleting_a_clinic_cascades(committing_db_session: AsyncSession) -> None:
    """DELTA G8. "We delete everything on request" must be demonstrably one statement."""
    session = committing_db_session
    clinic = await _make_clinic(session)
    await _minimal_booking_fixtures(session, clinic)
    await session.commit()

    await session.execute(text("delete from clinics where id = :id"), {"id": clinic.id})
    await session.commit()

    for table in ("slots", "services", "patients", "resources", "bookings"):
        remaining = await session.scalar(text(f"select count(*) from {table}"))
        assert remaining == 0, f"{table} still has rows after the clinic was deleted"


test_deleting_a_clinic_cascades = pytest.mark.db_commit(test_deleting_a_clinic_cascades)
