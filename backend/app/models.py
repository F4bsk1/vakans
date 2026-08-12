"""The canonical data model.

This is spec §2's DDL plus eight additive deltas, each documented in docs/SCHEMA_NOTES.md and
marked `DELTA Gn` below. The model is deliberately industry-neutral and identical to what
production will use, so the Kaddio/Muntra/BokaMera adapters replace only the *data source*
later — not the agent, engine or dashboard code.

Two conventions that are load-bearing:

- `DateTime(timezone=True)` everywhere. Never a bare DateTime. See app/timeutils.py.
- `MetaData(naming_convention=...)`. Without it Alembic autogenerate emits unnamed
  constraints and every future migration becomes a diff-noise fight.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A VARCHAR + CHECK column, matching spec §2's `text check (...)` exactly.

    Native PG enums are avoided on purpose: adding a value to one is a migration, and the
    demo will grow status values under time pressure.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda e: [m.value for m in e],
    )


def _uuid_pk() -> Mapped[UUID]:
    # Server default kept for spec fidelity, but seed/reset always supply the id from Python
    # so a given clinic + seed + day produces a byte-identical database.
    return mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))


# --- enums --------------------------------------------------------------------------------


class ResourceKind(StrEnum):
    PRACTITIONER = "practitioner"
    ROOM = "room"
    EQUIPMENT = "equipment"


class SlotStatus(StrEnum):
    OPEN = "open"
    BOOKED = "booked"
    BLOCKED = "blocked"
    # DELTA G6: reserved during the 90s refill SMS window, so the voice agent cannot book the
    # slot out from under a running cascade — precisely the moment the demo is built around.
    HELD = "held"


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NOSHOW = "noshow"


class WaitlistStatus(StrEnum):
    """DELTA G3: spec §2's waitlist holds only preferences, with nowhere to track a cascade."""

    ACTIVE = "active"
    OFFERED = "offered"
    FULFILLED = "fulfilled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class RefillOfferStatus(StrEnum):
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class SmsDirection(StrEnum):
    OUT = "out"
    IN = "in"


class SmsStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    RECEIVED = "received"


# --- tables -------------------------------------------------------------------------------


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    demo_phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # DELTA G7 -------------------------------------------------------------------------
    # URL-safe demo identifier shared by the CLI, the artifacts dir and the dashboard route.
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Never hardcode the timezone in a code path that may one day serve a Finnish clinic.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Europe/Stockholm")
    sms_sender_id: Mapped[str | None] = mapped_column(String(11))
    # The QA gate's teeth: /sim/reset refuses an ingestion-sourced profile with no reviewer.
    profile_reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    profile_reviewed_by: Mapped[str | None] = mapped_column(Text)
    seeded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    seed_fingerprint: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Resource(Base):
    """Practitioners, rooms, equipment. Names come from the scrape — real practitioner names
    are a large part of why the demo lands."""

    __tablename__ = "resources"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ResourceKind] = mapped_column(
        _enum(ResourceKind, "resource_kind"),
        nullable=False,
        server_default=ResourceKind.PRACTITIONER.value,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)  # 'Leg. fysioterapeut'

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (Index("ix_resources_clinic_id_sort_order", "clinic_id", "sort_order"),)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    # Bounded numeric: bare `numeric` renders 0.30000000000000004-style noise in the SEK
    # counter. Null means unknown, and the agent then says "kontakta oss" rather than guessing.
    price_sek: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    category: Mapped[str | None] = mapped_column(Text)
    bookable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    # Clinic sites often omit durations and the extraction prompt forbids inventing them, so
    # profile_mapping applies a per-category default and flags it. review.py surfaces these.
    duration_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    __table_args__ = (Index("ix_services_clinic_id", "clinic_id"),)


class ServiceResource(Base):
    """Which practitioners perform which service."""

    __tablename__ = "service_resources"

    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True
    )
    # DELTA G8: spec §2 has no clinic_id here, so a clinic-scoped reset would need a subselect
    # through services. Easy to get wrong, and it silently leaks rows across a reset.
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (Index("ix_service_resources_clinic_id", "clinic_id"),)


class Slot(Base):
    __tablename__ = "slots"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[SlotStatus] = mapped_column(
        _enum(SlotStatus, "slot_status"), nullable=False, server_default=SlotStatus.OPEN.value
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_slots_clinic_id_status_starts_at", "clinic_id", "status", "starts_at"),
        Index("ix_slots_clinic_id_resource_id_starts_at", "clinic_id", "resource_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
    )


class Patient(Base):
    """FAKE ONLY in the demo. `is_demo` plus app.phone.may_send_sms is what keeps a real SMS
    from reaching a stranger."""

    __tablename__ = "patients"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    noshow_risk: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default="0.100"
    )

    # DELTA G5: the risk heuristic (spec §5) needs "ny patient" and "tidigare no-show", and
    # neither existed as a column, so risk.py could not be fed from the database at all.
    prior_noshows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("noshow_risk >= 0 and noshow_risk <= 1", name="risk_range"),
        # Without this, /agent/book mints a duplicate patient on every call from the same
        # number, which destroys the "ny patient" risk signal.
        Index(
            "uq_patients_clinic_id_phone",
            "clinic_id",
            "phone",
            unique=True,
            postgresql_where=text("phone is not null"),
        ),
    )


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("slots.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"),
        nullable=False,
        server_default=BookingStatus.CONFIRMED.value,
    )
    booked_via: Mapped[str | None] = mapped_column(Text)  # 'voice' | 'refill' | 'seed'
    risk: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))  # snapshot at booking time
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # DELTA G2: /agent/reschedule and /agent/cancel take a booking_ref that spec §2 never
    # defines. Seeded bookings need one too — the demo caller phones about a seeded booking.
    # Six characters from a voice-safe alphabet; see app.booking_ref.
    booking_ref: Mapped[str] = mapped_column(String(12), nullable=False)
    # DELTA G5: the business timestamp the lead-time risk term reads. `created_at` stays
    # row-creation, so a reschedule does not silently reset the ">21 days ahead" signal.
    booked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("clinic_id", "booking_ref", name="uq_bookings_clinic_id_booking_ref"),
        # DELTA G1: nothing in spec §2 prevented two confirmed bookings on one slot. The voice
        # agent and an inbound refill "JA" racing for the same slot is a scenario the demo
        # deliberately creates. Partial, so cancelling correctly frees the slot again.
        Index(
            "uq_bookings_active_slot",
            "slot_id",
            unique=True,
            postgresql_where=text("status = 'confirmed'"),
        ),
        # Prompt rule 4 (spec §4) makes name+phone the primary lookup for an existing booking.
        Index("ix_bookings_clinic_id_patient_id_status", "clinic_id", "patient_id", "status"),
        Index("ix_bookings_clinic_id_status", "clinic_id", "status"),
    )


class Waitlist(Base):
    __tablename__ = "waitlist"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    earliest: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    latest: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    short_notice_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # DELTA G3: is this entry still live? Ranking uses created_at (wait time) + patient risk.
    status: Mapped[WaitlistStatus] = mapped_column(
        _enum(WaitlistStatus, "waitlist_status"),
        nullable=False,
        server_default=WaitlistStatus.ACTIVE.value,
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        Index("ix_waitlist_clinic_id_status_service_id", "clinic_id", "status", "service_id"),
    )


class RefillOffer(Base):
    """DELTA G3: one row per SMS offer in a refill cascade.

    Deliberately not folded into `waitlist`: a waitlist entry receives many offers over its
    life, and one cascade fans out across many entries. `cascade_id` groups a whole cascade
    and doubles as the correlation id the dashboard timeline groups on.

    Created empty in step 1 — the engine that fills it is step 3. It is in the baseline
    migration because /sim/reset must enumerate every clinic-scoped table, and adding tables
    later means editing reset plus a second migration.
    """

    __tablename__ = "refill_offers"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    slot_id: Mapped[UUID] = mapped_column(
        ForeignKey("slots.id", ondelete="CASCADE"), nullable=False
    )
    waitlist_id: Mapped[UUID] = mapped_column(
        ForeignKey("waitlist.id", ondelete="CASCADE"), nullable=False
    )
    patient_id: Mapped[UUID] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False
    )
    cascade_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    candidate_n: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RefillOfferStatus] = mapped_column(
        _enum(RefillOfferStatus, "refill_offer_status"),
        nullable=False,
        server_default=RefillOfferStatus.SENT.value,
    )
    sent_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "cascade_id", "candidate_n", name="uq_refill_offers_cascade_id_candidate_n"
        ),
        Index("ix_refill_offers_clinic_id_status", "clinic_id", "status"),
        Index("ix_refill_offers_slot_id", "slot_id"),
    )


class SmsMessage(Base):
    """DELTA G4: spec §3 has 46elks send + inbound webhook, but spec §2 stores nothing.

    Events alone will not do: inbound 46elks webhooks are retried, so an idempotency key is
    required, and an inbound "JA" must be resolvable to a live offer.
    """

    __tablename__ = "sms_messages"

    id: Mapped[UUID] = _uuid_pk()
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[SmsDirection] = mapped_column(
        _enum(SmsDirection, "sms_direction"), nullable=False
    )
    to_phone: Mapped[str] = mapped_column(Text, nullable=False)
    from_phone: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="fake")
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[SmsStatus] = mapped_column(
        _enum(SmsStatus, "sms_status"), nullable=False, server_default=SmsStatus.QUEUED.value
    )
    # Only this direction of the link exists, to keep refill_offers <-> sms_messages acyclic.
    refill_offer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("refill_offers.id", ondelete="SET NULL")
    )
    patient_id: Mapped[UUID | None] = mapped_column(ForeignKey("patients.id", ondelete="SET NULL"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # The webhook idempotency key. Partial, because outbound messages have no provider id
        # until the provider replies.
        Index(
            "uq_sms_messages_provider_provider_message_id",
            "provider",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id is not null"),
        ),
        Index("ix_sms_messages_clinic_id_created_at", "clinic_id", "created_at"),
    )


class Event(Base):
    """The action log: dashboard feed, ROI report and future compliance audit log at once.

    The charter is explicit that every state change writes here and nothing bypasses it.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    clinic_id: Mapped[UUID] = mapped_column(
        ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)  # see app.events.EventType
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sek_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # Groups a call or a whole refill cascade into one dashboard timeline. Adding it in step 5
    # would mean touching every emitter.
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid)
    # 'seed' | 'agent' | 'sim' | 'system' | 'webhook' — what makes the audit-log claim true.
    actor: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The dashboard tails by id; without this it seq-scans on demo day.
        Index("ix_events_clinic_id_id_desc", "clinic_id", text("id desc")),
        Index("ix_events_clinic_id_type_ts", "clinic_id", "type", "ts"),
    )


def enum_check_constraint_names() -> frozenset[str]:
    """Names of the CHECK constraints that SQLAlchemy's Enum type generates for us.

    These must be excluded from Alembic autogenerate comparison. SQLAlchemy marks them
    `_type_bound`, so it never emits them as standalone constraints, but the copy reflected
    back out of Postgres carries no such marker — so every autogenerate run would otherwise
    propose dropping all of them, and the model/migration drift test could never pass.
    """
    names: set[str] = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            type_ = col.type
            if isinstance(type_, Enum) and type_.create_constraint and type_.name:
                names.add(f"ck_{table.name}_{type_.name}")
    return frozenset(names)


def alembic_include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Autogenerate filter, shared by backend/alembic/env.py and the drift test.

    Both must use the same filter or they disagree about what "no drift" means.
    """
    return not (type_ == "check_constraint" and name in enum_check_constraint_names())


# Order matters for /sim/reset: children before parents. Keeping it next to the models means a
# new table cannot be added without this list being right there in the diff.
CLINIC_SCOPED_DELETE_ORDER: tuple[type[Base], ...] = (
    SmsMessage,
    RefillOffer,
    Booking,
    Waitlist,
    Slot,
    Patient,
    ServiceResource,
    Service,
    Resource,
    Event,
)
