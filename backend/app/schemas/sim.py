"""Request/response models for the demo-choreography endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
