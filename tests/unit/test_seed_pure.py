"""The seed generator's pure functions — no database involved."""

from __future__ import annotations

import itertools
import uuid
from datetime import date, time, timedelta

from app.fixtures import load_demo_profile
from app.profile_mapping import map_profile, resolve_duration
from app.schemas.clinic_profile import ServiceItem
from app.seed import (
    SeedConfig,
    assign_bookings,
    build_slot_grid,
    choose_demo_slots,
    det_uuid,
    make_fake_patients,
    make_rng,
    make_waitlist,
    slot_length_minutes,
)
from app.timeutils import DEFAULT_TZ, iso_week_start, local_naive_to_utc, to_local

STHLM = DEFAULT_TZ
CLINIC_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
FOUNDER = "+46701234567"


def _now(d: date = date(2026, 8, 12), t: time = time(9, 0)):  # Wednesday 09:00
    return local_naive_to_utc(d, t, STHLM)


def _grid(cfg: SeedConfig | None = None, now=None):
    cfg = cfg or SeedConfig(founder_phone=FOUNDER)
    now = now or _now()
    profile = load_demo_profile()
    mapped = map_profile(profile, max_practitioners=cfg.max_practitioners)
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    minutes = slot_length_minutes(profile, cfg)
    return (
        build_slot_grid(
            iso_week_start(to_local(now, STHLM).date()),
            mapped.resources,
            cfg,
            STHLM,
            slot_minutes=minutes,
            rng=rng,
        ),
        mapped,
        cfg,
        now,
    )


# --- determinism ------------------------------------------------------------------------------


def test_same_clinic_seed_and_day_produce_identical_output() -> None:
    a = make_rng(CLINIC_ID, SeedConfig(), _now(), STHLM)
    b = make_rng(CLINIC_ID, SeedConfig(), _now(), STHLM)
    assert [det_uuid(a) for _ in range(20)] == [det_uuid(b) for _ in range(20)]


def test_different_clinics_get_different_data():
    """Decision D5: two demos prepared the same day must not look like copies."""
    other = uuid.UUID("22222222-2222-4222-8222-222222222222")
    a = make_rng(CLINIC_ID, SeedConfig(), _now(), STHLM)
    b = make_rng(other, SeedConfig(), _now(), STHLM)
    assert [det_uuid(a) for _ in range(5)] != [det_uuid(b) for _ in range(5)]


def test_seeding_is_stable_across_processes() -> None:
    """Guards against anyone reaching for Python's hash(), which is salted per process."""
    rng = make_rng(CLINIC_ID, SeedConfig(rng_seed=20260907), _now(), STHLM)
    assert str(det_uuid(rng)) == str(
        det_uuid(make_rng(CLINIC_ID, SeedConfig(rng_seed=20260907), _now(), STHLM))
    )


# --- the grid ---------------------------------------------------------------------------------


def test_grid_is_business_days_only() -> None:
    slots, _, _, _ = _grid()
    for s in slots:
        assert to_local(s.starts_at, STHLM).weekday() < 5


def test_grid_stays_within_opening_hours_in_local_time() -> None:
    slots, _, cfg, _ = _grid()
    for s in slots:
        local_start = to_local(s.starts_at, STHLM)
        local_end = to_local(s.ends_at, STHLM)
        # Practitioners get +/-1h shifts, so allow one hour of slack either side.
        assert local_start.hour >= cfg.day_start.hour - 1
        assert local_end.hour <= cfg.day_end.hour + 1


def test_grid_skips_lunch() -> None:
    slots, _, cfg, _ = _grid()
    lunch_start, lunch_end = cfg.lunch
    for s in slots:
        local_start = to_local(s.starts_at, STHLM).time()
        local_end = to_local(s.ends_at, STHLM).time()
        assert not (local_start < lunch_end and local_end > lunch_start), f"{s} overlaps lunch"


def test_grid_covers_two_weeks() -> None:
    slots, _, _, _ = _grid()
    days = {to_local(s.starts_at, STHLM).date() for s in slots}
    assert len(days) == 10  # 2 weeks x Mon-Fri


def test_grid_slot_length_is_rounded_to_a_quarter_hour() -> None:
    """The demo profile's median duration is 37 minutes; a 37-minute calendar looks broken."""
    profile = load_demo_profile()
    assert profile.median_duration() == 37
    assert slot_length_minutes(profile, SeedConfig()) == 30


def test_grid_survives_the_spring_dst_week() -> None:
    """The transition is 29 Mar 2026 (a Sunday); the following Monday must still start at 08:00
    local, which a UTC-based day loop would get wrong."""
    slots, _, _, _ = _grid(now=_now(date(2026, 3, 25)))
    mondays = [s for s in slots if to_local(s.starts_at, STHLM).date() == date(2026, 3, 30)]
    assert mondays
    assert min(to_local(s.starts_at, STHLM).hour for s in mondays) == 8


def test_grid_survives_the_autumn_dst_week() -> None:
    slots, _, _, _ = _grid(now=_now(date(2026, 10, 21)))
    mondays = [s for s in slots if to_local(s.starts_at, STHLM).date() == date(2026, 10, 26)]
    assert mondays
    assert min(to_local(s.starts_at, STHLM).hour for s in mondays) == 8


def test_no_slot_overlaps_for_a_single_practitioner() -> None:
    slots, _, _, _ = _grid()
    by_resource: dict[int, list] = {}
    for s in slots:
        by_resource.setdefault(s.resource_index, []).append(s)
    for group in by_resource.values():
        group.sort(key=lambda s: s.starts_at)
        for prev, nxt in itertools.pairwise(group):
            assert prev.ends_at <= nxt.starts_at


# --- demo slots -------------------------------------------------------------------------------


def test_demo_slots_are_today_pm_and_tomorrow_am_on_a_normal_weekday() -> None:
    slots, _, cfg, now = _grid()
    future = [s for s in slots if s.ends_at > now]
    picked, strategy = choose_demo_slots(future, now, cfg, STHLM)

    assert strategy == "today_pm_tomorrow_am"
    assert 3 <= len(picked) <= 5  # spec §2 asks for 3-5
    days = {to_local(s.starts_at, STHLM).date() for s in picked}
    assert days == {date(2026, 8, 12), date(2026, 8, 13)}


def test_demo_slots_are_at_distinct_times() -> None:
    """Four practitioners free at 08:00 is four slots but only one option to offer a caller.

    The agent reads out up to three alternatives; three identical times sounds broken.
    """
    slots, _, cfg, now = _grid()
    future = [s for s in slots if s.ends_at > now]
    picked, _ = choose_demo_slots(future, now, cfg, STHLM)
    assert len({s.starts_at for s in picked}) == len(picked)


def test_demo_slots_are_at_distinct_times_in_the_fallback_branch() -> None:
    now = _now(date(2026, 8, 14), time(16, 0))  # Friday afternoon
    slots, _, cfg, _ = _grid(now=now)
    future = [s for s in slots if s.ends_at > now]
    picked, strategy = choose_demo_slots(future, now, cfg, STHLM)
    assert strategy == "next_business_days"
    assert len({s.starts_at for s in picked}) == len(picked)


def test_demo_slots_are_never_sooner_than_90_minutes_away() -> None:
    """A slot the caller could not physically reach is worse than no slot."""
    slots, _, cfg, now = _grid()
    future = [s for s in slots if s.ends_at > now]
    picked, _ = choose_demo_slots(future, now, cfg, STHLM)
    for s in picked:
        assert s.starts_at >= now + timedelta(minutes=90)


def test_demo_slots_fall_back_on_a_friday_afternoon() -> None:
    """Friday 16:00: there is no "today PM" left and tomorrow is Saturday."""
    now = _now(date(2026, 8, 14), time(16, 0))
    slots, _, cfg, _ = _grid(now=now)
    future = [s for s in slots if s.ends_at > now]
    picked, strategy = choose_demo_slots(future, now, cfg, STHLM)

    assert strategy == "next_business_days"
    assert picked
    assert all(to_local(s.starts_at, STHLM).weekday() < 5 for s in picked)


def test_demo_slots_fall_back_on_a_weekend() -> None:
    now = _now(date(2026, 8, 15), time(11, 0))  # Saturday
    slots, _, cfg, _ = _grid(now=now)
    future = [s for s in slots if s.ends_at > now]
    picked, strategy = choose_demo_slots(future, now, cfg, STHLM)

    assert strategy == "next_business_days"
    assert picked
    assert min(to_local(s.starts_at, STHLM).date() for s in picked) >= date(2026, 8, 17)


# --- patients, bookings, waitlist ---------------------------------------------------------------


def test_fake_patients_are_unique_and_swedish_mobiles() -> None:
    rng = make_rng(CLINIC_ID, SeedConfig(), _now(), STHLM)
    patients = make_fake_patients(rng, 40)
    assert len(patients) == 40
    assert len({p.phone for p in patients}) == 40
    assert len({p.name for p in patients}) == 40
    assert all(p.phone.startswith("+4670") for p in patients)
    assert all(p.is_demo for p in patients)


def test_booked_ratio_lands_near_the_target() -> None:
    slots, mapped, cfg, now = _grid()
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    patients = make_fake_patients(rng, cfg.patient_count)
    future = [s for s in slots if s.ends_at > now]
    bookings, _ = assign_bookings(
        rng, past=[], future=future, patients=patients, mapped=mapped, cfg=cfg, now=now, tz=STHLM
    )
    ratio = len(bookings) / len(future)
    assert 0.75 <= ratio <= 0.85


def test_past_slots_produce_noshow_history_for_the_risk_panel() -> None:
    """Without no-show history every future booking scores green and the panel looks like a stub."""
    slots, mapped, cfg, now = _grid()
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    patients = make_fake_patients(rng, cfg.patient_count)
    past = [s for s in slots if s.ends_at <= now]
    assert past, "Wednesday 09:00 must have elapsed slots earlier in the week"

    bookings, updated = assign_bookings(
        rng, past=past, future=[], patients=patients, mapped=mapped, cfg=cfg, now=now, tz=STHLM
    )
    statuses = {b.status.value for b in bookings}
    assert "completed" in statuses
    assert "noshow" in statuses
    assert sum(p.prior_noshows for p in updated) > 0


def test_exactly_one_waitlist_entry_uses_the_founder_mobile() -> None:
    slots, mapped, cfg, now = _grid()
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    patients = make_fake_patients(rng, cfg.patient_count)
    future = [s for s in slots if s.ends_at > now]
    demo_slots, _ = choose_demo_slots(future, now, cfg, STHLM)

    entries, founder = make_waitlist(
        rng, patients=patients, mapped=mapped, demo_slots=demo_slots, now=now, cfg=cfg
    )
    assert len(entries) == cfg.waitlist_size
    assert founder is not None
    assert founder.phone == FOUNDER
    assert sum(1 for p in patients if p.phone == FOUNDER) <= 1


def test_founder_lands_at_the_configured_cascade_rank() -> None:
    """Rank 2 by default, so the demo shows candidate #1 timing out and the cascade advancing
    to a phone that genuinely answers."""
    slots, mapped, cfg, now = _grid()
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    patients = make_fake_patients(rng, cfg.patient_count)
    future = [s for s in slots if s.ends_at > now]
    demo_slots, _ = choose_demo_slots(future, now, cfg, STHLM)

    entries, founder = make_waitlist(
        rng, patients=patients, mapped=mapped, demo_slots=demo_slots, now=now, cfg=cfg
    )
    # Ranking in step 3 is wait time first: oldest created_at wins.
    by_wait = sorted(entries, key=lambda w: w.created_at)
    assert by_wait[cfg.founder_rank - 1].patient_id == founder.id


def test_no_founder_entry_when_no_mobile_is_configured() -> None:
    cfg = SeedConfig(founder_phone=None)
    slots, mapped, _, now = _grid(cfg=cfg)
    rng = make_rng(CLINIC_ID, cfg, now, STHLM)
    patients = make_fake_patients(rng, cfg.patient_count)
    future = [s for s in slots if s.ends_at > now]
    demo_slots, _ = choose_demo_slots(future, now, cfg, STHLM)

    entries, founder = make_waitlist(
        rng, patients=patients, mapped=mapped, demo_slots=demo_slots, now=now, cfg=cfg
    )
    assert founder is None
    assert entries


# --- profile mapping ----------------------------------------------------------------------------


def test_published_durations_are_never_overridden() -> None:
    assert resolve_duration(ServiceItem(name="Nybesök", duration_min=45)) == (45, False)


def test_missing_duration_falls_back_by_category_and_is_flagged() -> None:
    duration, estimated = resolve_duration(ServiceItem(name="Behandling", category="massage"))
    assert (duration, estimated) == (60, True)


def test_followup_visits_get_a_shorter_default() -> None:
    duration, estimated = resolve_duration(ServiceItem(name="Återbesök naprapati"))
    assert (duration, estimated) == (30, True)


def test_unknown_category_uses_the_documented_fallback() -> None:
    assert resolve_duration(ServiceItem(name="Something", category="unknown")) == (45, True)


def test_practitioners_are_capped_for_a_readable_calendar() -> None:
    profile = load_demo_profile()
    mapped = map_profile(profile, max_practitioners=2)
    assert len(mapped.resources) == 2


def test_a_clinic_with_no_named_staff_still_gets_a_calendar() -> None:
    profile = load_demo_profile()
    profile.staff = []
    mapped = map_profile(profile)
    assert len(mapped.resources) == 1
    assert mapped.resources[0].name == "Behandlare"


def test_every_service_is_performable_by_someone() -> None:
    mapped = map_profile(load_demo_profile())
    for si in range(len(mapped.services)):
        assert any(s == si for s, _ in mapped.pairings), f"service {si} has no practitioner"


def test_practitioners_are_paired_to_services_they_actually_perform() -> None:
    """A naprapath offered for physiotherapy is what a clinic owner spots first in a demo.

    Substring matching used to produce exactly that: "Leg. fysioterapeut" is not a substring
    of "Nybesök fysioterapi", so nobody matched and the fallback assigned everyone.
    """
    profile = load_demo_profile()
    mapped = map_profile(profile)
    by_service = {
        mapped.services[si].name: {
            mapped.resources[ri].name for si2, ri in mapped.pairings if si2 == si
        }
        for si in range(len(mapped.services))
    }

    assert by_service["Nybesök fysioterapi"] == {"Johanna Lindqvist", "Priya Raman"}
    assert by_service["Nybesök naprapati"] == {"Marcus Ohlsson"}
    assert by_service["Klassisk massage 60 min"] == {"Erik Sandberg"}

    # The massage therapist must never be offered for a physiotherapy appointment.
    assert "Erik Sandberg" not in by_service["Nybesök fysioterapi"]
    assert "Marcus Ohlsson" not in by_service["Nybesök fysioterapi"]


def test_stem_matching_pairs_swedish_word_forms() -> None:
    """ "fysioterapi" and "fysioterapeut" are the same domain but neither prefixes the other."""
    from app.agent.matching import score_text

    assert score_text("fysioterapi", "Leg. fysioterapeut") > 0
    assert score_text("naprapati", "Leg. naprapat") > 0
    assert score_text("massage", "Massageterapeut") > 0
    # ...but unrelated words that merely start alike must not pair.
    assert score_text("fysik", "fysio") == 0
