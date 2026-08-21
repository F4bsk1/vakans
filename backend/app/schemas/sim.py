"""Request/response models for the demo-choreography endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CancelRandomRequest",
    "CancelRandomResponse",
    "CascadeStateOut",
    "ClinicSummary",
    "DemoSlotOut",
    "ResetRequest",
    "ResetResponse",
    "WaitlistReplyRequest",
    "WaitlistReplyResponse",
]


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int | None = None
    # Pinning `now` is what makes a rehearsal replayable: the demo slots depend on the wall
    # clock, so "same seed" only means "same demo" for a fixed now.
    now: datetime | None = None
    keep_events: bool = False
    booked_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    guaranteed_open: int | None = Field(default=None, ge=1, le=10)
    waitlist_size: int | None = Field(default=None, ge=1, le=50)


class DemoSlotOut(BaseModel):
    id: UUID
    starts_at: datetime
    ends_at: datetime
    resource_name: str
    speech: str  # pre-formatted Swedish, e.g. "imorgon kl 10 hos Johanna"


class ResetResponse(BaseModel):
    """Enough for an operator to confirm the demo is armed without opening the dashboard."""

    clinic_id: UUID
    slug: str
    fingerprint: str
    counts: dict[str, int]
    demo_slots: list[DemoSlotOut]
    demo_slot_strategy: str
    founder_waitlist_id: UUID | None
    risk_band_counts: dict[str, int]
    duration_ms: int


class CancelRandomRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    booking_ref: str | None = None
    # Default on, because a cascade with no candidates is a demo that visibly does nothing.
    # Turn it off to rehearse the "nobody wanted it" ending on purpose.
    prefer_with_candidates: bool = True
    # Pinnable for the same reason `reset` takes one: a rehearsal replayed against a sandbox
    # seeded for a fixed `now` has to be able to agree with it about what "upcoming" means.
    now: datetime | None = None


class CascadeStateOut(BaseModel):
    outcome: str
    cascade_id: UUID | None = None
    slot_id: UUID | None = None
    candidate_n: int | None = None
    patient_name: str | None = None
    expires_at: datetime | None = None
    remaining_candidates: int = 0
    reason: str | None = None


class CancelRandomResponse(BaseModel):
    booking_ref: str
    patient_name: str | None
    service: str | None
    resource_name: str | None
    starts_at: datetime
    speech: str  # what the slot looks like to a human: "imorgon kl 10 hos Johanna"
    sek_at_risk: float | None
    cascade: CascadeStateOut


class WaitlistReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omit both and the newest live offer in this clinic is answered — which is what the demo
    # operator wants nine times out of ten.
    cascade_id: UUID | None = None
    candidate_n: int | None = Field(default=None, ge=1)
    answer: str = "JA"
    # Spec §3 suggests 20 s. A delay makes the dashboard show the offer sitting there before it
    # resolves, which is the point of the beat.
    delay_s: int = Field(default=0, ge=0, le=300)
    now: datetime | None = None


class WaitlistReplyResponse(BaseModel):
    scheduled_in_s: int = 0
    accepted: bool = False
    answer: str | None = None
    reason: str | None = None
    cascade_id: UUID | None = None
    candidate_n: int | None = None
    patient_name: str | None = None
    booking_ref: str | None = None
    sek_value: float | None = None


class ClinicSummary(BaseModel):
    id: UUID
    slug: str
    name: str
    timezone: str
    demo_phone: str | None
    seeded_at: datetime | None
    seed_fingerprint: str | None
    profile_source: str | None
    profile_reviewed_at: datetime | None
