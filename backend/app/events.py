"""The event log and the dashboard pub/sub seam.

The charter is blunt about this table: "every state change writes to `events` (this table is
simultaneously dashboard feed, ROI report, and future compliance audit log — never bypass it)".

Three parts, and the split matters:

1. `emit()` writes the row on the **same session** as the state change and parks an envelope in
   an outbox on that session. It does not publish.
2. An `after_commit` hook drains the outbox to the bus. So a rolled-back transaction never
   emits a phantom dashboard event — a bug that is genuinely awful to chase during a live demo.
3. The bus is a `Protocol`. `InProcessEventBus` is all a single-process demo needs (decision
   D3), and swapping in a Postgres LISTEN/NOTIFY implementation later requires no caller
   changes.

Step 1 only emits `seed_completed` and `sim_reset`, but building the seam now removes a
refactor from steps 3 and 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import Event
from app.timeutils import now_utc

logger = logging.getLogger("vakans.events")

OUTBOX_KEY = "vakans_event_outbox"
QUEUE_MAXSIZE = 256


class EventType(StrEnum):
    """Spec §6 — "dashboardens språk"."""

    CALL_STARTED = "call_started"
    CALL_TRANSCRIPT = "call_transcript"
    BOOKING_CREATED = "booking_created"
    BOOKING_RESCHEDULED = "booking_rescheduled"
    BOOKING_CANCELLED = "booking_cancelled"
    SLOT_FREED = "slot_freed"
    REFILL_SMS_SENT = "refill_sms_sent"
    REFILL_REPLY = "refill_reply"
    SLOT_REFILLED = "slot_refilled"
    ESCALATION = "escalation"
    SMS_SENT = "sms_sent"
    MATCH_NETWORK = "match_network"

    # Additive sandbox/ops events, not in spec §6. Flagged as an extension so nobody assumes
    # the spec listed them.
    SEED_COMPLETED = "seed_completed"
    SIM_RESET = "sim_reset"
    INGESTION_COMPLETED = "ingestion_completed"
    # Spec §6 names the three happy-path refill events but neither of the two ways a cascade
    # stops without a booking. A dashboard told a cascade started and never told it finished
    # shows a slot blinking red forever, which reads as a hung demo rather than "nobody wanted
    # it". See app/refill/engine.py.
    REFILL_OFFER_EXPIRED = "refill_offer_expired"
    REFILL_CASCADE_ENDED = "refill_cascade_ended"


class EventEnvelope(BaseModel):
    """What the WebSocket ships. Mirrors the row and carries nothing secret."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    clinic_id: UUID
    ts: Any
    type: str
    payload: dict[str, Any]
    sek_value: Decimal | None = None
    correlation_id: UUID | None = None
    actor: str | None = None


class EventBus(Protocol):
    def publish_nowait(self, ev: EventEnvelope) -> None: ...

    def subscribe(self, clinic_id: UUID) -> Any: ...


class InProcessEventBus:
    """Single-process fan-out. Decision D3's default; roughly the 50 lines the charter predicted.

    Queues are bounded and drop the oldest event on overflow: a stalled dashboard tab must
    never apply backpressure to a live phone call.
    """

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: dict[UUID, set[asyncio.Queue[EventEnvelope]]] = {}

    def publish_nowait(self, ev: EventEnvelope) -> None:
        # Deliberately synchronous: this is called from SQLAlchemy's after_commit hook, which
        # is sync code running inside the greenlet. asyncio.Queue.put_nowait needs no running
        # loop, so there is no loop juggling here and nothing can block the commit.
        for queue in self._subscribers.get(ev.clinic_id, ()):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                logger.warning("event queue full for clinic %s, dropped oldest", ev.clinic_id)
            queue.put_nowait(ev)

    @contextlib.asynccontextmanager
    async def subscribe(self, clinic_id: UUID) -> AsyncIterator[AsyncIterator[EventEnvelope]]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.setdefault(clinic_id, set()).add(queue)

        async def stream() -> AsyncIterator[EventEnvelope]:
            while True:
                yield await queue.get()

        try:
            yield stream()
        finally:
            subs = self._subscribers.get(clinic_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    del self._subscribers[clinic_id]

    def subscriber_count(self, clinic_id: UUID) -> int:
        return len(self._subscribers.get(clinic_id, ()))


_bus: EventBus = InProcessEventBus()


def get_event_bus() -> EventBus:
    return _bus


def set_event_bus(bus: EventBus) -> None:
    """Swap the bus. For tests, and for the day a PgNotifyEventBus replaces the default."""
    global _bus
    _bus = bus


@event.listens_for(Session, "after_commit")
def _drain_outbox(session: Session) -> None:
    """Publish only what actually committed."""
    outbox: list[EventEnvelope] = session.info.pop(OUTBOX_KEY, [])
    bus = get_event_bus()
    for envelope in outbox:
        try:
            bus.publish_nowait(envelope)
        except Exception:  # a broken subscriber must never break the transaction
            logger.exception("failed to publish event %s", envelope.id)


@event.listens_for(Session, "after_rollback")
def _discard_outbox(session: Session) -> None:
    session.info.pop(OUTBOX_KEY, None)


async def emit(
    session: AsyncSession,
    *,
    clinic_id: UUID,
    type: EventType,
    payload: Mapping[str, Any],
    sek_value: Decimal | None = None,
    correlation_id: UUID | None = None,
    actor: str = "system",
) -> Event:
    """Write an event on the caller's session and queue it for post-commit publication.

    Must be called with the *same* session as the state change it describes, so the two commit
    or roll back together.
    """
    row = Event(
        clinic_id=clinic_id,
        ts=now_utc(),
        type=type.value,
        payload=dict(payload),
        sek_value=sek_value,
        correlation_id=correlation_id,
        actor=actor,
    )
    session.add(row)
    # Flush so the bigserial id exists: the dashboard reconnects with `after_id`, so an
    # envelope without a real id would be useless.
    await session.flush()

    envelope = EventEnvelope.model_validate(row)
    session.sync_session.info.setdefault(OUTBOX_KEY, []).append(envelope)
    return row


async def recent_events(
    session: AsyncSession,
    clinic_id: UUID,
    *,
    after_id: int | None = None,
    limit: int = 200,
) -> list[EventEnvelope]:
    """The dashboard's initial load and its reconnect-with-`after_id` path (step 5)."""
    stmt = select(Event).where(Event.clinic_id == clinic_id)
    if after_id is not None:
        stmt = stmt.where(Event.id > after_id)
    stmt = stmt.order_by(Event.id.desc()).limit(limit)

    rows = (await session.execute(stmt)).scalars().all()
    return [EventEnvelope.model_validate(r) for r in reversed(rows)]
