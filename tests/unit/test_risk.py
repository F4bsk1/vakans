"""The no-show heuristic (spec §5), term by term."""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.risk import (
    BASE,
    CLAMP_MAX,
    RiskInput,
    explain,
    is_bad_slot_time,
    is_long_lead_time,
    noshow_risk,
    requires_confirmation,
    risk_band,
)
from app.timeutils import DEFAULT_TZ, local_naive_to_utc

STHLM = DEFAULT_TZ


def _input(
    *,
    day: date = date(2026, 8, 19),  # a Wednesday: no slot-of-week penalty
    at: time = time(10, 0),
    lead_days: int = 3,
    is_new: bool = False,
    prior_noshows: int = 0,
) -> RiskInput:
    starts_at = local_naive_to_utc(day, at, STHLM)
    return RiskInput(
        booked_at=starts_at - timedelta(days=lead_days),
        starts_at=starts_at,
        tz=STHLM,
        is_new_patient=is_new,
        prior_noshows=prior_noshows,
    )


def test_baseline_only() -> None:
    assert noshow_risk(_input()) == pytest.approx(BASE)


def test_each_term_in_isolation() -> None:
    assert noshow_risk(_input(lead_days=30)) == pytest.approx(0.23)  # 0.08 + 0.15
    assert noshow_risk(_input(is_new=True)) == pytest.approx(0.28)  # 0.08 + 0.20
    assert noshow_risk(_input(prior_noshows=1)) == pytest.approx(0.23)  # 0.08 + 0.15
    # Monday 08:00 -> bad slot
    assert noshow_risk(_input(day=date(2026, 8, 17), at=time(8, 0))) == pytest.approx(0.18)


def test_all_terms_together_max_out_at_0_68() -> None:
    """Worth knowing: the spec's four terms cannot reach the 0.85 clamp, so the demo can never
    display a risk above 0.68."""
    risk = noshow_risk(
        _input(day=date(2026, 8, 17), at=time(8, 0), lead_days=30, is_new=True, prior_noshows=2)
    )
    assert risk == pytest.approx(0.68)
    assert risk < CLAMP_MAX


def test_lead_time_boundary_is_strict() -> None:
    assert not is_long_lead_time(*_two(21))  # exactly 21 days does not count
    assert is_long_lead_time(*_two(22))


def _two(days: int) -> tuple:
    starts_at = local_naive_to_utc(date(2026, 9, 30), time(10, 0), STHLM)
    return starts_at - timedelta(days=days), starts_at


@pytest.mark.parametrize(
    ("day", "at", "expected"),
    [
        (date(2026, 8, 17), time(8, 0), True),  # Monday 08:00 - inclusive lower bound
        (date(2026, 8, 17), time(8, 59), True),
        (date(2026, 8, 17), time(9, 0), False),  # exclusive upper bound
        (date(2026, 8, 17), time(7, 59), False),
        (date(2026, 8, 21), time(15, 0), True),  # Friday 15:00
        (date(2026, 8, 21), time(16, 59), True),
        (date(2026, 8, 21), time(17, 0), False),
        (date(2026, 8, 21), time(14, 59), False),
        (date(2026, 8, 19), time(8, 30), False),  # Wednesday morning is fine
    ],
)
def test_bad_slot_windows_are_half_open(day: date, at: time, expected: bool) -> None:
    assert is_bad_slot_time(local_naive_to_utc(day, at, STHLM), STHLM) is expected


def test_bad_slot_is_evaluated_in_clinic_local_time_across_dst() -> None:
    """A Friday 15:30 appointment is penalised in both summer and winter.

    If this were computed in UTC, the same wall-clock slot would fall in or out of the window
    depending on the season — a bug that only shows up half the year.
    """
    summer = local_naive_to_utc(date(2026, 7, 24), time(15, 30), STHLM)  # UTC+2 -> 13:30Z
    winter = local_naive_to_utc(date(2026, 1, 23), time(15, 30), STHLM)  # UTC+1 -> 14:30Z
    assert is_bad_slot_time(summer, STHLM)
    assert is_bad_slot_time(winter, STHLM)


@pytest.mark.parametrize(
    ("risk", "band"),
    [
        (0.0, "green"),
        (0.199, "green"),
        (0.20, "yellow"),  # boundary belongs to the higher band
        (0.449, "yellow"),
        (0.45, "red"),
        (0.85, "red"),
    ],
)
def test_band_boundaries(risk: float, band: str) -> None:
    assert risk_band(risk) == band


def test_only_red_requires_confirmation() -> None:
    assert requires_confirmation(0.45)
    assert not requires_confirmation(0.44)


def test_prior_noshows_is_binary_not_scaled() -> None:
    """Spec says "tidigare no-show", not "how many"."""
    one = noshow_risk(_input(prior_noshows=1))
    five = noshow_risk(_input(prior_noshows=5))
    assert one == five


def test_explain_lists_the_contributing_terms() -> None:
    terms = explain(_input(day=date(2026, 8, 21), at=time(16, 0), is_new=True))
    labels = [label for label, _ in terms]
    assert labels == ["grundrisk", "måndag morgon eller fredag eftermiddag", "ny patient"]
    assert sum(w for _, w in terms) == pytest.approx(
        noshow_risk(_input(day=date(2026, 8, 21), at=time(16, 0), is_new=True))
    )


def test_risk_always_within_clamp() -> None:
    for lead in (0, 10, 40):
        for new in (True, False):
            for prior in (0, 3):
                r = noshow_risk(_input(lead_days=lead, is_new=new, prior_noshows=prior))
                assert 0.0 <= r <= CLAMP_MAX
