"""Time handling, with the DST transitions that would otherwise break exactly one demo week."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.timeutils import (
    DEFAULT_TZ,
    business_days,
    is_wall_clock_unambiguous,
    iso_week_start,
    local_day_bounds,
    local_naive_to_utc,
    now_utc,
    speak_date_sv,
    speak_time_sv,
    to_local,
)

STHLM = DEFAULT_TZ


def test_now_utc_is_aware_and_utc() -> None:
    assert now_utc().tzinfo is UTC


@pytest.mark.parametrize(
    ("day", "expected_utc_hour"),
    [
        # Winter: Sweden is UTC+1, so local 08:00 stores as 07:00Z.
        (date(2026, 1, 19), 7),
        # Summer: UTC+2, so local 08:00 stores as 06:00Z.
        (date(2026, 7, 20), 6),
        # The Monday *before* the spring transition (29 Mar 2026) is still UTC+1...
        (date(2026, 3, 23), 7),
        # ...and the Monday after it is UTC+2. A UTC-based day loop gets one of these wrong.
        (date(2026, 3, 30), 6),
        # Same story around the autumn transition (25 Oct 2026).
        (date(2026, 10, 19), 6),
        (date(2026, 10, 26), 7),
    ],
)
def test_local_0800_maps_to_the_right_utc_hour(day: date, expected_utc_hour: int) -> None:
    stored = local_naive_to_utc(day, time(8, 0), STHLM)
    assert stored.tzinfo is UTC
    assert stored.hour == expected_utc_hour
    # And it must round-trip back to 08:00 wall clock, which is the property that matters.
    assert to_local(stored, STHLM).hour == 8


def test_a_dst_week_is_23_or_25_hours_long() -> None:
    """local_day_bounds must not assume every day is 24 h."""
    spring_start, spring_end = local_day_bounds(date(2026, 3, 29), STHLM)
    assert (spring_end - spring_start).total_seconds() / 3600 == 23

    autumn_start, autumn_end = local_day_bounds(date(2026, 10, 25), STHLM)
    assert (autumn_end - autumn_start).total_seconds() / 3600 == 25


def test_clinic_hours_never_hit_a_dst_transition() -> None:
    """The invariant that lets the slot grid ignore ambiguity: Swedish transitions are at
    02:00–03:00 on a Sunday, and clinics open Mon–Fri 08–17."""
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        for hour in range(8, 18):
            assert is_wall_clock_unambiguous(day, time(hour, 0), STHLM)


def test_the_repeated_autumn_hour_is_flagged() -> None:
    # 02:30 on 25 Oct 2026 happens twice. The guard must catch it.
    assert not is_wall_clock_unambiguous(date(2026, 10, 25), time(2, 30), STHLM)


def test_the_skipped_spring_hour_is_flagged() -> None:
    # 02:30 on 29 Mar 2026 does not exist.
    assert not is_wall_clock_unambiguous(date(2026, 3, 29), time(2, 30), STHLM)


def test_iso_week_start_is_monday() -> None:
    assert iso_week_start(date(2026, 8, 12)) == date(2026, 8, 10)  # Wed -> Mon
    assert iso_week_start(date(2026, 8, 10)) == date(2026, 8, 10)  # Mon -> itself
    assert iso_week_start(date(2026, 8, 16)) == date(2026, 8, 10)  # Sun -> that Mon


def test_business_days_skips_weekends() -> None:
    days = list(business_days(date(2026, 8, 12), weeks=2))
    assert len(days) == 10
    assert days[0] == date(2026, 8, 10)
    assert days[-1] == date(2026, 8, 21)
    assert all(d.weekday() < 5 for d in days)


def test_naive_datetimes_are_rejected() -> None:
    # Ruff's DTZ rules stop us writing these, but data can still arrive naive from elsewhere.
    with pytest.raises(ValueError, match="naive"):
        to_local(datetime(2026, 8, 20, 10, 0), STHLM)  # noqa: DTZ001


def test_speak_date_sv() -> None:
    dt = local_naive_to_utc(date(2026, 8, 20), time(9, 0), STHLM)
    assert speak_date_sv(dt, STHLM) == "torsdag 20 augusti"


@pytest.mark.parametrize(
    ("target_day", "target_time", "expected"),
    [
        (date(2026, 8, 12), time(14, 30), "idag kl 14:30"),
        (date(2026, 8, 12), time(15, 0), "idag kl 15"),
        (date(2026, 8, 13), time(10, 0), "imorgon kl 10"),
        (date(2026, 8, 14), time(9, 0), "i övermorgon kl 9"),
        (date(2026, 8, 17), time(8, 0), "på måndag kl 8"),
        # Beyond a week, say the date outright rather than risk "which Thursday?".
        (date(2026, 8, 27), time(9, 0), "torsdag 27 augusti kl 9"),
    ],
)
def test_speak_time_sv(target_day: date, target_time: time, expected: str) -> None:
    now = local_naive_to_utc(date(2026, 8, 12), time(9, 0), STHLM)  # Wednesday
    dt = local_naive_to_utc(target_day, target_time, STHLM)
    assert speak_time_sv(dt, STHLM, now=now) == expected


def test_speak_time_sv_uses_clinic_local_time_not_utc() -> None:
    """A slot stored as 06:00Z in July must be spoken as 08:00, not 06:00."""
    dt = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    now = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)
    assert speak_time_sv(dt, STHLM, now=now) == "idag kl 8"


def test_other_timezones_are_supported() -> None:
    """clinics.timezone exists so this is a data change, not a code change (delta G7)."""
    helsinki = ZoneInfo("Europe/Helsinki")
    stored = local_naive_to_utc(date(2026, 7, 20), time(8, 0), helsinki)
    assert stored.hour == 5  # Finland is UTC+3 in summer
