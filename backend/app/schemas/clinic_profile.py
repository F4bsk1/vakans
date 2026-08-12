"""The `clinic_profile` contract — spec §5.

This schema is shared by both halves of the build order: `app.seed` reads a hardcoded profile
(step 1) and `ingestion.extract` produces one from a real clinic site (step 2). It lives in
`app`, not `ingestion`, because `ingestion` may depend on `app` but never the reverse.

Beyond spec §5 it carries a provenance layer (`price_evidence`, `*_source_url`). Those fields
are the mechanical teeth behind "hitta ALDRIG på priser": `ingestion.merge.scrub_unverified`
deletes any price whose evidence quote is not literally present on the page it cites. A prompt
rule alone gets roughly 90% compliance, and the remaining 10% is a dead deal — the spec says
so itself: "Fel pris i demo = död affär".
"""

from __future__ import annotations

import statistics
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class BookingSystemHint(StrEnum):
    """Which booking system the clinic already runs. Spec §5: "styr er adapterpitch"."""

    KADDIO = "kaddio"
    MUNTRA = "muntra"
    BOKADIREKT = "bokadirekt"
    BOKAMERA = "bokamera"
    TIMECENTER = "timecenter"
    CLINIKO = "cliniko"
    EASYPRACTICE = "easypractice"
    WEBBOKNING = "webbokning"
    CUSTOM = "custom"
    NONE = "none"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class OpeningHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: Weekday
    open: time | None = None
    close: time | None = None
    closed: bool = False


class StaffMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None = None
    specialties: list[str] = Field(default_factory=list)
    source_url: str | None = None


class ServiceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    # Nullable here even though `services.duration_min` is NOT NULL in the database. Clinic
    # sites frequently omit durations and the extraction prompt forbids inventing them, so
    # `app.profile_mapping` applies a documented per-category default and flags it as
    # estimated. See docs/SCHEMA_NOTES.md.
    duration_min: int | None = None
    price_sek: float | None = None
    category: str | None = None
    description: str | None = None
    bookable: bool = True

    # Provenance. Required for any price to survive scrub_unverified().
    price_source_url: str | None = None
    price_evidence: str | None = None

    @field_validator("price_sek")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError("price_sek cannot be negative")
        return v


class Policies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancellation: str | None = None
    late_fee: str | None = None
    insurance_accepted: list[str] = Field(default_factory=list)
    referral_needed: bool | None = None


class Practical(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parking: str | None = None
    transit: str | None = None
    accessibility: str | None = None


class FaqItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    q: str
    # Required by spec §5: "varje faq-post ska ha source_url".
    a: str
    source_url: str


class ClinicProfile(BaseModel):
    """Everything the agent, the seed generator and the dashboard know about a clinic.

    `extra="forbid"` is deliberate: under structured extraction an invented key should be a
    loud validation failure that triggers a retry, not data silently dropped on the floor.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"

    name: str
    city: str | None = None
    address: str | None = None
    phone: str | None = None
    email: EmailStr | None = None
    booking_system_hint: BookingSystemHint | None = None
    opening_hours: list[OpeningHours] = Field(default_factory=list)
    staff: list[StaffMember] = Field(default_factory=list)
    services: list[ServiceItem] = Field(default_factory=list)
    policies: Policies = Field(default_factory=Policies)
    practical: Practical = Field(default_factory=Practical)
    faq: list[FaqItem] = Field(default_factory=list)
    confidence_notes: str = ""

    # --- helpers used by seed, validate and the agent prompt -------------------------------

    def priced_services(self) -> list[ServiceItem]:
        return [s for s in self.services if s.price_sek is not None]

    def all_prices_have_sources(self) -> bool:
        return all(
            s.price_source_url and s.price_evidence
            for s in self.services
            if s.price_sek is not None
        )

    def median_duration(self, default: int = 45) -> int:
        """Median published duration, used as the sandbox slot length."""
        known = [s.duration_min for s in self.services if s.duration_min]
        if not known:
            return default
        return int(statistics.median(known))

    def practitioners(self) -> list[StaffMember]:
        return list(self.staff)

    def compact(self) -> dict[str, Any]:
        """The `{compact_profile_json}` injected into the agent system prompt (spec §4).

        Provenance fields are stripped: they exist for the human QA gate, and every token in
        the system prompt costs latency on a live call.
        """
        return {
            "name": self.name,
            "city": self.city,
            "address": self.address,
            "phone": self.phone,
            "opening_hours": [
                oh.model_dump(mode="json", exclude_none=True) for oh in self.opening_hours
            ],
            "staff": [
                {"name": s.name, "title": s.title, "specialties": s.specialties} for s in self.staff
            ],
            "services": [
                {
                    "name": s.name,
                    "duration_min": s.duration_min,
                    "price_sek": s.price_sek,
                    "category": s.category,
                }
                for s in self.services
                if s.bookable
            ],
            "policies": self.policies.model_dump(mode="json", exclude_none=True),
            "practical": self.practical.model_dump(mode="json", exclude_none=True),
            "faq": [{"q": f.q, "a": f.a} for f in self.faq],
        }

    # --- spec-shaped JSON ------------------------------------------------------------------
    # Spec §5 wraps the object in {"clinic_profile": {...}}. Artifacts on disk match the spec
    # exactly; internal code passes the flat model around.

    def to_spec_json(self) -> dict[str, Any]:
        return {"clinic_profile": self.model_dump(mode="json", exclude_none=True)}

    @classmethod
    def from_spec_json(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data.get("clinic_profile", data))


class ProfileMeta(BaseModel):
    """Where a profile came from and whether a human has signed off on it."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["hardcoded", "ingestion", "manual"]
    base_url: str | None = None
    crawled_at: datetime | None = None
    pages_used: list[str] = Field(default_factory=list)
    extractor: str | None = None  # e.g. "pydantic-ai:mistral:mistral-large" or "fake"
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ClinicProfileDocument(BaseModel):
    """What is stored in `clinics.profile` (jsonb) and written to artifacts/<slug>/profile.json."""

    model_config = ConfigDict(extra="forbid")

    profile: ClinicProfile
    meta: ProfileMeta

    @property
    def is_reviewed(self) -> bool:
        return self.meta.reviewed_at is not None
