"""The events table and the pub/sub seam."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import (
    EventEnvelope,
    EventType,
    InProcessEventBus,
    emit,
    get_event_bus,
    recent_events,
    set_event_bus,
)
from app.fixtures import load_demo_profile_document
from app.models import Clinic

# How long to wait before concluding that nothing is going to arrive. Generous enough not to
# flake on a loaded machine, short enough that the suite stays fast.
SETTLE = 0.05


@contextlib.asynccontextmanager
async def collecting(
    bus: InProcessEventBus, clinic_id: uuid.UUID
) -> AsyncIterator[list[EventEnvelope]]:
    """Drain a subscription into a list from a background task.

    Deliberately *not* `await asyncio.wait_for(anext(stream), ...)`: cancelling anext() on an
    async generator leaves the generator unusable, so a timeout-then-read-again test would be
    exercising a broken generator rather than the bus.
    """
    received: list[EventEnvelope] = []
    async with bus.subscribe(clinic_id) as stream:

        async def consume() -> None:
            async for ev in stream:
                received.append(ev)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let the consumer reach its first await
        try:
            yield received
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.fixture
def bus() -> InProcessEventBus:
    original = get_event_bus()
    fresh = InProcessEventBus()
    set_event_bus(fresh)
    yield fresh
    set_event_bus(original)


async def _clinic(session: AsyncSession, slug: str = "demo") -> Clinic:
    doc = load_demo_profile_document()
    clinic = Clinic(
        id=uuid.uuid4(), name=doc.profile.name, slug=slug, profile=doc.model_dump(mode="json")
    )
    session.add(clinic)
    await session.flush()
    return clinic


async def test_emit_writes_a_row_with_an_id(db_session: AsyncSession) -> None:
    clinic = await _clinic(db_session)
    row = await emit(
        db_session,
        clinic_id=clinic.id,
        type=EventType.SEED_COMPLETED,
        payload={"slots": 700},
        actor="seed",
    )
    assert row.id > 0  # the dashboard reconnects using this id
    assert row.actor == "seed"
    assert row.payload == {"slots": 700}


async def test_recent_events_is_chronological_and_scoped(db_session: AsyncSession) -> None:
    a = await _clinic(db_session, slug="a")
    b = await _clinic(db_session, slug="b")

    for i in range(3):
        await emit(db_session, clinic_id=a.id, type=EventType.SMS_SENT, payload={"n": i})
    await emit(db_session, clinic_id=b.id, type=EventType.SMS_SENT, payload={"n": 99})

    events = await recent_events(db_session, a.id)
    assert [e.payload["n"] for e in events] == [0, 1, 2]  # oldest first
    assert all(e.clinic_id == a.id for e in events)


async def test_recent_events_after_id_supports_reconnect(db_session: AsyncSession) -> None:
    clinic = await _clinic(db_session)
    first = await emit(db_session, clinic_id=clinic.id, type=EventType.SMS_SENT, payload={"n": 0})
    await emit(db_session, clinic_id=clinic.id, type=EventType.SMS_SENT, payload={"n": 1})

    events = await recent_events(db_session, clinic.id, after_id=first.id)
    assert [e.payload["n"] for e in events] == [1]


async def test_sek_value_keeps_two_decimals(db_session: AsyncSession) -> None:
    """The refill SEK counter must not render floating-point noise."""
    clinic = await _clinic(db_session)
    row = await emit(
        db_session,
        clinic_id=clinic.id,
        type=EventType.SLOT_REFILLED,
        payload={},
        sek_value=Decimal("895.00"),
    )
    await db_session.refresh(row)
    assert row.sek_value == Decimal("895.00")


@pytest.mark.db_commit
async def test_events_publish_only_after_commit(
    committing_db_session: AsyncSession, bus: InProcessEventBus
) -> None:
    session = committing_db_session
    clinic = await _clinic(session)
    await session.commit()

    async with collecting(bus, clinic.id) as received:
        await emit(session, clinic_id=clinic.id, type=EventType.SLOT_FREED, payload={"x": 1})
        await asyncio.sleep(SETTLE)
        # Nothing may reach the bus before the transaction commits.
        assert received == []

        await session.commit()
        await asyncio.sleep(SETTLE)
        assert len(received) == 1

    assert received[0].type == EventType.SLOT_FREED
    assert received[0].payload == {"x": 1}


@pytest.mark.db_commit
async def test_rolled_back_events_never_publish(
    committing_db_session: AsyncSession, bus: InProcessEventBus
) -> None:
    """A phantom dashboard event from a rolled-back transaction is agonising to debug live."""
    session = committing_db_session
    clinic = await _clinic(session)
    await session.commit()
    # Capture before the rollback: rollback expires every instance in the session, and reading
    # an expired attribute from async code is a sync lazy-load that raises MissingGreenlet.
    clinic_id = clinic.id

    async with collecting(bus, clinic_id) as received:
        await emit(session, clinic_id=clinic_id, type=EventType.SLOT_FREED, payload={"x": 1})
        await session.rollback()
        await asyncio.sleep(SETTLE)
        assert received == []

    assert await recent_events(session, clinic_id) == []


def _envelope(clinic_id: uuid.UUID, n: int) -> EventEnvelope:
    return EventEnvelope(id=n, clinic_id=clinic_id, ts=None, type="sms_sent", payload={"n": n})


async def test_bus_is_scoped_per_clinic(bus: InProcessEventBus) -> None:
    """Decision D5: two demos running at once must not see each other's events."""
    a, b = uuid.uuid4(), uuid.uuid4()
    async with collecting(bus, a) as received:
        bus.publish_nowait(_envelope(b, 1))
        await asyncio.sleep(SETTLE)
        assert received == []


async def test_bus_drops_oldest_when_a_subscriber_stalls() -> None:
    """A stalled dashboard tab must never apply backpressure to a live phone call."""
    small = InProcessEventBus(maxsize=2)
    clinic_id = uuid.uuid4()

    async with small.subscribe(clinic_id) as stream:
        for n in range(4):
            small.publish_nowait(_envelope(clinic_id, n))
        # Capacity 2, so the two oldest were dropped rather than blocking the publisher.
        got = [(await anext(stream)).payload["n"] for _ in range(2)]

    assert got == [2, 3]


async def test_unsubscribe_cleans_up(bus: InProcessEventBus) -> None:
    clinic_id = uuid.uuid4()
    async with bus.subscribe(clinic_id):
        assert bus.subscriber_count(clinic_id) == 1
    assert bus.subscriber_count(clinic_id) == 0
