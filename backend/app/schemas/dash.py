"""What the dashboard reads — spec §3 "Dashboard".

Two shapes only: `DashState` is the whole picture on load, and `EventEnvelope` (from
`app.events`) is every change after that. The dashboard never polls and never derives state it
was not given — if a number appears on screen, it came from one of these two payloads.

Everything human-facing is **pre-rendered on the server**: Swedish weekday names, clock times,
"kr" amounts. Same reason the agent tools return `speech`: the backend owns Swedish, and a
client that never formats a time cannot format one wrongly. It also keeps the browser free of
timezone logic, which is where dashboards usually go wrong an hour at a time.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.events import EventEnvelope


class DashClinic(BaseModel):
    id: UUID
    slug: str
    name: str
    timezone: str
    demo_phone: str | None
    seeded_at: datetime | None


class DashResource(BaseModel):
    """A practitioner column in the calendar."""

    id: UUID
    name: str
    first_name: str  # what the agent says out loud, and what fits a column header
    title: str | None
    sort_order: int


class DashSlot(BaseModel):
    id: UUID
    resource_id: UUID
    starts_at: datetime
    ends_at: datetime
    label: str  # "08:00" — local clock, rendered server-side
    status: str  # open | booked | held | blocked

    booking_ref: str | None = None
    # confirmed | completed | noshow. A past slot keeps its booking, and a no-show is the
    # single most valuable thing on this calendar — it is the loss the product claims to fix,
    # so the dashboard must be able to point at one.
    booking_status: str | None = None
    patient_name: str | None = None
    service: str | None = None
    price_sek: float | None = None
    risk: float | None = None
    risk_band: str | None = None  # green | yellow | red
    booked_via: str | None = None  # voice | refill | seed
    is_past: bool = False


class DashDay(BaseModel):
    date: str  # local ISO date, the calendar's column key
    label: str  # "mån 24 aug"
    is_today: bool
    slots: list[DashSlot]


class DashKpis(BaseModel):
    """The five numbers a clinic owner actually reacts to."""

    slots_total: int
    slots_open: int
    slots_booked: int
    slots_held: int
    fill_rate: float  # 0-1

    bookings_by_voice: int
    bookings_by_refill: int

    # The ROI story, in kronor. `recovered` is money that would have been lost; `at_risk` is
    # money currently sitting in a freed slot nobody has claimed yet.
    recovered_sek: float
    at_risk_sek: float

    risk_green: int
    risk_yellow: int
    risk_red: int


class DashOffer(BaseModel):
    candidate_n: int
    patient_name: str | None
    status: str  # sent | accepted | declined | expired | superseded
    sent_at: datetime
    expires_at: datetime
    responded_at: datetime | None


class DashCascade(BaseModel):
    """One refill cascade, as a timeline the dashboard can animate down."""

    cascade_id: UUID
    slot_id: UUID
    starts_at: datetime
    slot_label: str  # "på måndag kl 8 hos Priya"
    service: str | None
    price_sek: float | None
    is_live: bool  # an offer is out and its window has not closed
    expires_at: datetime | None
    outcome: str  # live | refilled | exhausted
    offers: list[DashOffer]


class DashState(BaseModel):
    """`GET /dash/{clinic_id}/state`. Everything the dashboard needs to render from cold."""

    clinic: DashClinic
    generated_at: datetime
    # The dashboard opens the WebSocket with `?after_id=` set to this, so an event emitted
    # between the snapshot and the socket opening is replayed rather than lost.
    last_event_id: int
    # The visible calendar range: today forward, not the calendar week. See routers/dash.py.
    range_start: str
    range_end: str
    range_label: str
    resources: list[DashResource]
    days: list[DashDay]
    kpis: DashKpis
    cascades: list[DashCascade]
    events: list[EventEnvelope]
