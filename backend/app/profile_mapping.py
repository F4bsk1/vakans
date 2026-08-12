"""`ClinicProfile` -> resources, services and their pairing.

This is where the spec §5 / §2 mismatch on durations is resolved: the profile allows
`duration_min = null` (the extraction prompt forbids inventing one), but `services.duration_min`
is NOT NULL. We apply a documented per-category default and set `duration_is_estimated`, which
`review.py` then surfaces so a human sees every guess. See docs/SCHEMA_NOTES.md, ambiguity 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.models import ResourceKind
from app.schemas.clinic_profile import ClinicProfile, ServiceItem, StaffMember

# Per-category fallbacks, in minutes. Deliberately conservative: a slot that is a bit too long
# looks like a calm clinic, a slot that is too short makes the calendar look broken.
DURATION_DEFAULTS: dict[str, int] = {
    "fysio": 45,
    "fysioterapi": 45,
    "sjukgymnastik": 45,
    "naprapati": 45,
    "kiropraktik": 30,
    "massage": 60,
    "estetik": 60,
    "hudvard": 60,
    "akupunktur": 45,
}
FALLBACK_DURATION = 45

# "Återbesök"/"uppföljning" visits are shorter than a first visit almost everywhere.
FOLLOWUP_PATTERN = re.compile(
    r"\b(återbesök|aterbesok|uppföljning|uppfoljning|fortsättning)\b", re.I
)
FOLLOWUP_DURATION = 30
NEW_VISIT_PATTERN = re.compile(r"\b(nybesök|nybesok|första besöket|forsta besoket)\b", re.I)

# A calendar with more than four columns is unreadable on a demo screen.
MAX_PRACTITIONERS = 4


@dataclass(frozen=True)
class ResourceSpec:
    name: str
    title: str | None
    kind: ResourceKind
    sort_order: int
    specialties: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    duration_min: int
    price_sek: Decimal | None
    category: str | None
    bookable: bool
    duration_is_estimated: bool


@dataclass(frozen=True)
class MappedProfile:
    resources: list[ResourceSpec] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    # (service index, resource index) pairs
    pairings: list[tuple[int, int]] = field(default_factory=list)


def resolve_duration(item: ServiceItem) -> tuple[int, bool]:
    """(duration_min, is_estimated). Published durations always win."""
    if item.duration_min:
        return item.duration_min, False

    name = item.name or ""
    if FOLLOWUP_PATTERN.search(name) and not NEW_VISIT_PATTERN.search(name):
        return FOLLOWUP_DURATION, True

    if item.category:
        key = item.category.strip().lower()
        if key in DURATION_DEFAULTS:
            return DURATION_DEFAULTS[key], True

    return FALLBACK_DURATION, True


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _matches_specialty(service: ServiceItem, staff: StaffMember) -> bool:
    """Loose keyword overlap between a service and a practitioner's stated specialties."""
    haystack = _normalise(f"{service.name} {service.category or ''}")
    for specialty in (*staff.specialties, staff.title or ""):
        token = _normalise(specialty)
        if len(token) >= 4 and token in haystack:
            return True
    return False


def map_profile(
    profile: ClinicProfile, *, max_practitioners: int = MAX_PRACTITIONERS
) -> MappedProfile:
    """Build the resource/service rows a sandbox needs from a clinic profile."""
    staff = profile.staff[:max_practitioners]
    resources = [
        ResourceSpec(
            name=s.name,
            title=s.title,
            kind=ResourceKind.PRACTITIONER,
            sort_order=i,
            specialties=tuple(s.specialties),
        )
        for i, s in enumerate(staff)
    ]
    # A clinic site with no named staff still needs a calendar to exist.
    if not resources:
        resources = [
            ResourceSpec(
                name="Behandlare", title=None, kind=ResourceKind.PRACTITIONER, sort_order=0
            )
        ]
        staff = []

    services: list[ServiceSpec] = []
    for item in profile.services:
        duration, estimated = resolve_duration(item)
        services.append(
            ServiceSpec(
                name=item.name,
                duration_min=duration,
                price_sek=Decimal(str(item.price_sek)) if item.price_sek is not None else None,
                category=item.category,
                bookable=item.bookable,
                duration_is_estimated=estimated,
            )
        )

    pairings: list[tuple[int, int]] = []
    for si, item in enumerate(profile.services):
        matched = [ri for ri, s in enumerate(staff) if _matches_specialty(item, s)]
        # No specialty signal means everybody can perform it. Better an over-broad calendar
        # than a service nobody can be booked for.
        for ri in matched or range(len(resources)):
            pairings.append((si, ri))

    return MappedProfile(resources=resources, services=services, pairings=pairings)
