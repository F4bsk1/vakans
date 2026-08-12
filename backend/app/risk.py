"""No-show risk — the rule model from spec §5.

    risk = 0.08
         + 0.15 * (booked more than 21 days in advance)
         + 0.10 * (Monday 08–09 or Friday 15–17)
         + 0.20 * (new patient)
         + 0.15 * (has a previous no-show)

Clamped to 0–0.85.

This is deliberately a heuristic, not a model, and the demo says so out loud — the charter
calls for "heuristic + honest labeling". `explain()` exists so the risk panel can show *why*
a booking is amber ("ny patient + fredag eftermiddag") rather than just a number; that
sentence is the actual sales moment, and it makes the honesty framing concrete.

Pure: no database, no clock, no I/O. Everything comes in through `RiskInput`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

BASE = 0.08
W_LEAD_TIME = 0.15
W_BAD_SLOT = 0.10
W_NEW_PATIENT = 0.20
W_HISTORY = 0.15

CLAMP_MIN = 0.0
CLAMP_MAX = 0.85

LEAD_TIME_THRESHOLD = timedelta(days=21)

# Band thresholds from spec §5: <0.2 green, <0.45 yellow, otherwise red + "bekräftelsekrav".
GREEN_BELOW = 0.20
YELLOW_BELOW = 0.45

RiskBand = Literal["green", "yellow", "red"]


@dataclass(frozen=True)
class RiskInput:
    booked_at: datetime  # aware; the business booking time, not row creation
    starts_at: datetime  # aware
    tz: ZoneInfo  # clinic-local timezone, for the slot-of-week term
    is_new_patient: bool
    prior_noshows: int = 0


def is_long_lead_time(booked_at: datetime, starts_at: datetime) -> bool:
    """Strictly more than 21 days ahead. Spec says "bokad >21 dgr i förväg"."""
    return (starts_at - booked_at) > LEAD_TIME_THRESHOLD


def is_bad_slot_time(starts_at: datetime, tz: ZoneInfo) -> bool:
    """Monday 08:00–09:00 or Friday 15:00–17:00, in **clinic-local** time.

    Half-open intervals, and local rather than UTC on purpose: the behavioural claim is about
    the patient's Monday morning, which does not shift by an hour twice a year.
    """
    local = starts_at.astimezone(tz)
    t = local.time()
    weekday = local.weekday()
    if weekday == 0:  # Monday
        return time(8, 0) <= t < time(9, 0)
    if weekday == 4:  # Friday
        return time(15, 0) <= t < time(17, 0)
    return False


def explain(inp: RiskInput) -> list[tuple[str, float]]:
    """Term-by-term breakdown, Swedish labels, ready for the dashboard risk panel."""
    terms: list[tuple[str, float]] = [("grundrisk", BASE)]
    if is_long_lead_time(inp.booked_at, inp.starts_at):
        terms.append(("bokad mer än 21 dagar i förväg", W_LEAD_TIME))
    if is_bad_slot_time(inp.starts_at, inp.tz):
        terms.append(("måndag morgon eller fredag eftermiddag", W_BAD_SLOT))
    if inp.is_new_patient:
        terms.append(("ny patient", W_NEW_PATIENT))
    if inp.prior_noshows > 0:
        terms.append(("tidigare uteblivet besök", W_HISTORY))
    return terms


def noshow_risk(inp: RiskInput) -> float:
    """The score, rounded to 3 dp to match `numeric(4,3)` in the database."""
    total = sum(weight for _, weight in explain(inp))
    return round(min(max(total, CLAMP_MIN), CLAMP_MAX), 3)


def risk_band(risk: float) -> RiskBand:
    if risk < GREEN_BELOW:
        return "green"
    if risk < YELLOW_BELOW:
        return "yellow"
    return "red"


def requires_confirmation(risk: float) -> bool:
    """Red bookings get the "bekräftelsekrav" flag — the differentiated-reminder hook."""
    return risk_band(risk) == "red"
