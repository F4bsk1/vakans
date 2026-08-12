"""The sandbox generator — spec §2 "Seed-regler".

Design: **pure functions that produce specs, plus one thin database writer.** That is what
makes the interesting logic unit-testable without a database and reproducible across machines.

Determinism is a hard requirement, not a nicety. The same clinic, seed and day must produce a
byte-identical sandbox, so a rehearsal can be replayed and two clinics seeded on the same day
never collide (decision D5). Every random draw comes from one `random.Random` seeded with a
sha256 of `(clinic_id, seed, date)` — never Python's `hash()`, which is salted per process.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.booking_ref import new_booking_ref
from app.events import EventType, emit
from app.models import (
    Booking,
    BookingStatus,
    Clinic,
    Patient,
    Resource,
    Service,
    ServiceResource,
    Slot,
    SlotStatus,
    Waitlist,
    WaitlistStatus,
)
from app.profile_mapping import MappedProfile, ResourceSpec, map_profile
from app.risk import RiskInput, noshow_risk, risk_band
from app.schemas.clinic_profile import ClinicProfile
from app.seed_data import FIRST_NAMES, SURNAMES
from app.timeutils import business_days, get_tz, iso_week_start, local_naive_to_utc, to_local

DemoSlotStrategy = Literal["today_pm_tomorrow_am", "next_business_days"]


@dataclass(frozen=True)
class SeedConfig:
    rng_seed: int = 20260907
    weeks: int = 2  # current + next (spec §2)
    day_start: time = time(8, 0)
    day_end: time = time(17, 0)
    lunch: tuple[time, time] = (time(12, 0), time(13, 0))
    slot_minutes: int | None = None  # None -> derived from the profile
    booked_ratio: float = 0.80
    past_fill_ratio: float = 0.90
    past_noshow_rate: float = 0.10
    guaranteed_open: int = 4  # spec says 3-5
    waitlist_size: int = 9  # spec says 8-10
    patient_count: int = 40
    max_practitioners: int = 4
    founder_phone: str | None = None
    founder_rank: int = 2  # where the JA-svararen lands in the cascade


@dataclass(frozen=True)
class SlotSpec:
    id: UUID
    resource_index: int
    starts_at: datetime
    ends_at: datetime
    status: SlotStatus = SlotStatus.OPEN


@dataclass(frozen=True)
class PatientSpec:
    id: UUID
    name: str
    phone: str
    prior_noshows: int = 0
    noshow_risk: float = 0.1
    is_demo: bool = True


@dataclass(frozen=True)
class BookingSpec:
    id: UUID
    slot_id: UUID
    service_index: int
    patient_id: UUID
    status: BookingStatus
    booking_ref: str
    booked_at: datetime
    risk: float


@dataclass(frozen=True)
class WaitlistSpec:
    id: UUID
    patient_id: UUID
    service_index: int
    earliest: datetime
    latest: datetime
    short_notice_ok: bool
    created_at: datetime
    priority: int


@dataclass(frozen=True)
class SeedResult:
    counts: dict[str, int]
    fingerprint: str
    demo_slot_ids: list[UUID]
    demo_slot_strategy: DemoSlotStrategy
    founder_waitlist_id: UUID | None
    risk_band_counts: dict[str, int]


# --- deterministic primitives ---------------------------------------------------------------


def make_rng(clinic_id: UUID, cfg: SeedConfig, now: datetime, tz: ZoneInfo) -> random.Random:
    """One seeded Random per (clinic, seed, local date).

    sha256 rather than `hash()`: Python's string hash is salted per process, so `hash()` would
    make "same seed, same demo" false across restarts.
    """
    local_day = to_local(now, tz).date().isoformat()
    material = f"{clinic_id}:{cfg.rng_seed}:{local_day}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(material).digest()[:8], "big"))


def det_uuid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def fingerprint(*parts: object) -> str:
    """Stable short digest of the generated data, for "did this reset produce the same demo?"."""
    blob = json.dumps(parts, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def slot_length_minutes(profile: ClinicProfile, cfg: SeedConfig) -> int:
    """Grid step. Rounded to a quarter hour: a calendar of 37-minute slots looks broken."""
    if cfg.slot_minutes:
        return cfg.slot_minutes
    raw = profile.median_duration()
    return max(30, round(raw / 15) * 15)


# --- pure generators ------------------------------------------------------------------------


def build_slot_grid(
    week_start: date,
    resources: list[ResourceSpec],
    cfg: SeedConfig,
    tz: ZoneInfo,
    *,
    slot_minutes: int,
    rng: random.Random,
) -> list[SlotSpec]:
    """Mon-Fri 08:00-17:00 local, minus lunch, per practitioner, for `cfg.weeks` weeks.

    Iterates over **local dates** and converts each wall-clock time to UTC. Iterating in UTC
    and adding timedelta(days=1) would drift by an hour across a DST transition, turning a
    Monday 08:00 slot into 07:00 or 09:00 local in exactly one demo week.
    """
    # Slight per-practitioner variation so the calendar looks like a real clinic rather than a
    # generated grid. Fixed per resource, so it stays stable across the fortnight.
    offsets = [
        (rng.choice([0, 0, 0, 60]), rng.choice([0, 0, 0, -60])) for _ in range(len(resources))
    ]

    specs: list[SlotSpec] = []
    lunch_start, lunch_end = cfg.lunch

    for day in business_days(week_start, cfg.weeks):
        for ri in range(len(resources)):
            start_shift, end_shift = offsets[ri]
            day_start = _shift(cfg.day_start, start_shift)
            day_end = _shift(cfg.day_end, end_shift)

            cursor = datetime.combine(day, day_start)
            end_of_day = datetime.combine(day, day_end)
            while cursor + timedelta(minutes=slot_minutes) <= end_of_day:
                slot_end = cursor + timedelta(minutes=slot_minutes)
                # Skip anything overlapping the lunch hour.
                if not (
                    cursor.time() < lunch_end
                    and slot_end.time() > lunch_start
                    and cursor.date() == day
                ):
                    specs.append(
                        SlotSpec(
                            id=det_uuid(rng),
                            resource_index=ri,
                            starts_at=local_naive_to_utc(day, cursor.time(), tz),
                            ends_at=local_naive_to_utc(day, slot_end.time(), tz),
                        )
                    )
                cursor = slot_end

    specs.sort(key=lambda s: (s.starts_at, s.resource_index))
    return specs


def _shift(t: time, minutes: int) -> time:
    base = datetime(2000, 1, 1, t.hour, t.minute) + timedelta(minutes=minutes)  # noqa: DTZ001
    return base.time()


def choose_demo_slots(
    future: list[SlotSpec], now: datetime, cfg: SeedConfig, tz: ZoneInfo
) -> tuple[list[SlotSpec], DemoSlotStrategy]:
    """Hand-pick the open slots the demo will actually use.

    Spec §2 asks for 3-5 open slots at "bra demotider (idag em, imorgon fm)". That is a
    *separate* guarantee from the ~80% fill, not the same one.

    The obvious version breaks in three ways that all happen in practice: a demo on a Friday
    afternoon, a demo booked for a Monday morning (so "today PM" is already gone), and a demo
    prepared over the weekend. Hence the explicit fallback and the reported strategy, so the
    operator knows which slots are armed without guessing.
    """
    local_now = to_local(now, tz)
    today = local_now.date()
    tomorrow = today + timedelta(days=1)
    # Never offer a slot the caller could not realistically reach.
    earliest = now + timedelta(minutes=90)

    today_pm = [
        s
        for s in future
        if to_local(s.starts_at, tz).date() == today
        and to_local(s.starts_at, tz).hour >= 13
        and s.starts_at >= earliest
    ]
    tomorrow_am = [
        s
        for s in future
        if to_local(s.starts_at, tz).date() == tomorrow and 8 <= to_local(s.starts_at, tz).hour < 11
    ]

    if today_pm and tomorrow_am:
        from_today = _spread_over_time(today_pm, 2)
        from_tomorrow = _spread_over_time(tomorrow_am, cfg.guaranteed_open - len(from_today))
        return from_today + from_tomorrow, "today_pm_tomorrow_am"

    # Fallback: mornings of the next business days that actually exist in the grid.
    mornings: list[SlotSpec] = []
    seen_days: list[date] = []
    for s in future:
        local = to_local(s.starts_at, tz)
        if s.starts_at < earliest or not (8 <= local.hour < 12):
            continue
        if local.date() not in seen_days:
            if len(seen_days) >= 2:
                break
            seen_days.append(local.date())
        mornings.append(s)

    return _spread_over_time(mornings, cfg.guaranteed_open), "next_business_days"


def _spread_over_time(slots: list[SlotSpec], n: int) -> list[SlotSpec]:
    """Pick up to `n` slots at *distinct* start times, preferring the earliest of each.

    Without this, the picker returns the same clock time across every practitioner — four
    "imorgon kl 8"s. The agent's availability tool reads out up to three options, and three
    identical times sounds broken to a caller and unimpressive to the clinic owner watching.
    Falls back to same-time slots only if there genuinely are not enough distinct ones.
    """
    if n <= 0:
        return []

    picked: list[SlotSpec] = []
    seen_times: set[datetime] = set()
    for s in slots:
        if s.starts_at not in seen_times:
            seen_times.add(s.starts_at)
            picked.append(s)
            if len(picked) == n:
                return picked

    # Not enough distinct times available; top up with whatever is left.
    for s in slots:
        if s not in picked:
            picked.append(s)
            if len(picked) == n:
                break
    return picked


def make_fake_patients(rng: random.Random, n: int) -> list[PatientSpec]:
    """Fabricated patients. Never real people, and every one is flagged `is_demo`."""
    used_names: set[str] = set()
    used_phones: set[str] = set()
    patients: list[PatientSpec] = []

    while len(patients) < n:
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(SURNAMES)}"
        if name in used_names:
            continue
        used_names.add(name)
        # +4670 + 7 digits. Sweden has no reserved test range, which is exactly why
        # app.phone.may_send_sms exists — these numbers may well belong to real people.
        phone = f"+4670{rng.randint(1000000, 9999999)}"
        if phone in used_phones:
            continue
        used_phones.add(phone)
        patients.append(PatientSpec(id=det_uuid(rng), name=name, phone=phone))

    return patients


def _service_for(rng: random.Random, mapped: MappedProfile, resource_index: int) -> int:
    """A service this practitioner can actually perform."""
    allowed = [si for si, ri in mapped.pairings if ri == resource_index]
    return rng.choice(allowed) if allowed else 0


def assign_bookings(
    rng: random.Random,
    *,
    past: list[SlotSpec],
    future: list[SlotSpec],
    patients: list[PatientSpec],
    mapped: MappedProfile,
    cfg: SeedConfig,
    now: datetime,
    tz: ZoneInfo,
) -> tuple[list[BookingSpec], list[PatientSpec]]:
    """Bookings for elapsed and upcoming slots, plus the patients with updated history.

    Past slots are what make the risk panel believable: they produce `completed` and `noshow`
    history, and without any no-shows every future booking scores green and the panel looks
    like a stub.
    """
    prior: dict[UUID, int] = {p.id: 0 for p in patients}
    latest_risk: dict[UUID, float] = {}
    seen: set[UUID] = set()
    bookings: list[BookingSpec] = []
    used_refs: set[str] = set()

    def _ref() -> str:
        while True:
            ref = new_booking_ref(rng)
            if ref not in used_refs:
                used_refs.add(ref)
                return ref

    def _book(slot: SlotSpec, status: BookingStatus) -> None:
        patient = rng.choice(patients)
        service_index = _service_for(rng, mapped, slot.resource_index)
        lead_days = rng.choice([1, 2, 3, 5, 7, 10, 14, 21, 25, 30, 40])
        booked_at = slot.starts_at - timedelta(days=lead_days)
        is_new = patient.id not in seen
        seen.add(patient.id)

        risk = noshow_risk(
            RiskInput(
                booked_at=booked_at,
                starts_at=slot.starts_at,
                tz=tz,
                is_new_patient=is_new,
                prior_noshows=prior[patient.id],
            )
        )
        latest_risk[patient.id] = risk
        if status is BookingStatus.NOSHOW:
            prior[patient.id] += 1

        bookings.append(
            BookingSpec(
                id=det_uuid(rng),
                slot_id=slot.id,
                service_index=service_index,
                patient_id=patient.id,
                status=status,
                booking_ref=_ref(),
                booked_at=booked_at,
                risk=risk,
            )
        )

    # Past first, so that no-show history exists before future risk is scored.
    for slot in past:
        if rng.random() > cfg.past_fill_ratio:
            continue
        status = (
            BookingStatus.NOSHOW if rng.random() < cfg.past_noshow_rate else BookingStatus.COMPLETED
        )
        _book(slot, status)

    fillable = list(future)
    rng.shuffle(fillable)
    target = int(len(fillable) * cfg.booked_ratio)
    for slot in fillable[:target]:
        _book(slot, BookingStatus.CONFIRMED)

    updated = [
        replace(p, prior_noshows=prior[p.id], noshow_risk=latest_risk.get(p.id, 0.1))
        for p in patients
    ]
    return bookings, updated


def make_waitlist(
    rng: random.Random,
    *,
    patients: list[PatientSpec],
    mapped: MappedProfile,
    demo_slots: list[SlotSpec],
    now: datetime,
    cfg: SeedConfig,
) -> tuple[list[WaitlistSpec], PatientSpec | None]:
    """Waitlist entries, one of which is the founder's real mobile ("JA-svararen").

    The founder entry is placed at `cfg.founder_rank` (default 2) so the demo shows candidate
    #1 timing out and the cascade advancing to a phone that genuinely answers. That beat is the
    whole point of the refill demo. Set `founder_rank=1` if rehearsals show the 90s timeout
    drags.
    """
    if not demo_slots:
        return [], None

    entries: list[WaitlistSpec] = []
    pool = [p for p in patients]
    rng.shuffle(pool)
    chosen = pool[: cfg.waitlist_size]

    founder: PatientSpec | None = None
    if cfg.founder_phone and chosen:
        rank = min(max(cfg.founder_rank, 1), len(chosen)) - 1
        founder = replace(chosen[rank], name=f"{chosen[rank].name} (demo)", phone=cfg.founder_phone)
        chosen[rank] = founder

    # Staggered creation times: wait time is half the ranking key in step 3, so ordering must
    # be deterministic rather than dependent on insert order.
    for i, patient in enumerate(chosen):
        created_at = now - timedelta(days=cfg.waitlist_size - i, hours=rng.randint(0, 12))
        target = demo_slots[i % len(demo_slots)]
        entries.append(
            WaitlistSpec(
                id=det_uuid(rng),
                patient_id=patient.id,
                service_index=_service_for(rng, mapped, target.resource_index),
                # A window wide enough to cover the demo slots, which is what makes the
                # cascade actually find candidates.
                earliest=now - timedelta(days=1),
                latest=target.starts_at + timedelta(days=7),
                short_notice_ok=rng.random() < 0.85,
                created_at=created_at,
                priority=0,
            )
        )

    return entries, founder


# --- the database writer ---------------------------------------------------------------------


async def seed_clinic(
    session: AsyncSession,
    clinic: Clinic,
    *,
    now: datetime,
    cfg: SeedConfig,
) -> SeedResult:
    """Generate a full sandbox fortnight for one clinic.

    Assumes the caller has already deleted this clinic's existing sandbox rows (see
    `routers/sim.reset`) and is inside a transaction.
    """
    profile = ClinicProfile.model_validate(clinic.profile["profile"])
    tz = get_tz(clinic.timezone)
    rng = make_rng(clinic.id, cfg, now, tz)

    mapped = map_profile(profile, max_practitioners=cfg.max_practitioners)
    slot_minutes = slot_length_minutes(profile, cfg)

    # --- resources, services, pairings
    resource_ids = [det_uuid(rng) for _ in mapped.resources]
    service_ids = [det_uuid(rng) for _ in mapped.services]

    await session.execute(
        insert(Resource),
        [
            {
                "id": rid,
                "clinic_id": clinic.id,
                "kind": spec.kind,
                "name": spec.name,
                "title": spec.title,
                "active": True,
                "sort_order": spec.sort_order,
            }
            for rid, spec in zip(resource_ids, mapped.resources, strict=True)
        ],
    )
    if mapped.services:
        await session.execute(
            insert(Service),
            [
                {
                    "id": sid,
                    "clinic_id": clinic.id,
                    "name": spec.name,
                    "duration_min": spec.duration_min,
                    "price_sek": spec.price_sek,
                    "category": spec.category,
                    "bookable": spec.bookable,
                    "duration_is_estimated": spec.duration_is_estimated,
                }
                for sid, spec in zip(service_ids, mapped.services, strict=True)
            ],
        )
    if mapped.pairings:
        await session.execute(
            insert(ServiceResource),
            [
                {
                    "service_id": service_ids[si],
                    "resource_id": resource_ids[ri],
                    "clinic_id": clinic.id,
                }
                for si, ri in mapped.pairings
            ],
        )

    # --- slots
    week_start = iso_week_start(to_local(now, tz).date())
    all_slots = build_slot_grid(
        week_start, mapped.resources, cfg, tz, slot_minutes=slot_minutes, rng=rng
    )
    past = [s for s in all_slots if s.ends_at <= now]
    future = [s for s in all_slots if s.ends_at > now]

    demo_slots, strategy = choose_demo_slots(future, now, cfg, tz)
    demo_ids = {s.id for s in demo_slots}
    # Force-opened: excluded from the fill so they are guaranteed available.
    fillable = [s for s in future if s.id not in demo_ids]

    # --- patients and bookings
    patients = make_fake_patients(rng, cfg.patient_count)
    bookings, patients = assign_bookings(
        rng,
        past=past,
        future=fillable,
        patients=patients,
        mapped=mapped,
        cfg=cfg,
        now=now,
        tz=tz,
    )

    waitlist, founder = make_waitlist(
        rng, patients=patients, mapped=mapped, demo_slots=demo_slots, now=now, cfg=cfg
    )
    if founder is not None:
        patients = [founder if p.id == founder.id else p for p in patients]

    await session.execute(
        insert(Patient),
        [
            {
                "id": p.id,
                "clinic_id": clinic.id,
                "name": p.name,
                "phone": p.phone,
                "is_demo": True,
                "noshow_risk": Decimal(str(round(p.noshow_risk, 3))),
                "prior_noshows": p.prior_noshows,
                "first_seen_at": now - timedelta(days=90),
                "created_at": now,
            }
            for p in patients
        ],
    )

    booked_slot_ids = {b.slot_id for b in bookings if b.status is not BookingStatus.CANCELLED}
    await session.execute(
        insert(Slot),
        [
            {
                "id": s.id,
                "clinic_id": clinic.id,
                "resource_id": resource_ids[s.resource_index],
                "starts_at": s.starts_at,
                "ends_at": s.ends_at,
                "status": SlotStatus.BOOKED if s.id in booked_slot_ids else SlotStatus.OPEN,
            }
            for s in all_slots
        ],
    )

    if bookings:
        await session.execute(
            insert(Booking),
            [
                {
                    "id": b.id,
                    "clinic_id": clinic.id,
                    "slot_id": b.slot_id,
                    "service_id": service_ids[b.service_index],
                    "patient_id": b.patient_id,
                    "status": b.status,
                    "booked_via": "seed",
                    "risk": Decimal(str(b.risk)),
                    "booking_ref": b.booking_ref,
                    "booked_at": b.booked_at,
                }
                for b in bookings
            ],
        )

    if waitlist:
        await session.execute(
            insert(Waitlist),
            [
                {
                    "id": w.id,
                    "clinic_id": clinic.id,
                    "patient_id": w.patient_id,
                    "service_id": service_ids[w.service_index],
                    "earliest": w.earliest,
                    "latest": w.latest,
                    "short_notice_ok": w.short_notice_ok,
                    "created_at": w.created_at,
                    "status": WaitlistStatus.ACTIVE,
                    "priority": w.priority,
                }
                for w in waitlist
            ],
        )

    # --- summary
    bands = {"green": 0, "yellow": 0, "red": 0}
    for b in bookings:
        if b.status is BookingStatus.CONFIRMED:
            bands[risk_band(b.risk)] += 1

    counts = {
        "resources": len(mapped.resources),
        "services": len(mapped.services),
        "slots": len(all_slots),
        "patients": len(patients),
        "bookings": len(bookings),
        "waitlist": len(waitlist),
        "open_slots": len(all_slots) - len(booked_slot_ids),
        "demo_slots": len(demo_slots),
    }
    fp = fingerprint(
        sorted(str(s.id) for s in all_slots),
        sorted(b.booking_ref for b in bookings),
        sorted(str(w.id) for w in waitlist),
        counts,
    )
    founder_waitlist_id = None
    if founder is not None:
        founder_waitlist_id = next((w.id for w in waitlist if w.patient_id == founder.id), None)

    result = SeedResult(
        counts=counts,
        fingerprint=fp,
        demo_slot_ids=[s.id for s in demo_slots],
        demo_slot_strategy=strategy,
        founder_waitlist_id=founder_waitlist_id,
        risk_band_counts=bands,
    )

    # One event, not one per booking: a few hundred booking_created events would flood the
    # dashboard on every reset and bury whatever the demo is actually showing.
    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.SEED_COMPLETED,
        payload={
            "counts": counts,
            "fingerprint": fp,
            "demo_slot_strategy": strategy,
            "risk_bands": bands,
            "slot_minutes": slot_minutes,
        },
        actor="seed",
    )
    return result
