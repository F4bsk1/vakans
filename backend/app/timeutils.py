"""Time handling and Swedish time-of-day speech.

**The rule, stated once:** every timestamp is stored as `timestamptz` and handled in Python as
a timezone-aware **UTC** datetime. Local (Europe/Stockholm) times exist at exactly two
boundaries:

  (a) constructing a slot grid from a business rule like "mån–fre 08–17", and
  (b) rendering a time for a human — the dashboard, an SMS body, or the agent's `speech`.

The `speak_*` functions are the *only* place Swedish time strings are produced. The charter's
latency trick — the backend pre-formats what the agent says, so the LLM never has to render a
Swedish date — depends on that staying true.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Europe/Stockholm")

# Lowercase: these are read aloud and appear mid-sentence ("på torsdag", "imorgon kl 10").
WEEKDAYS_SV = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")
MONTHS_SV = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)


def now_utc() -> datetime:
    """The only clock entry point in the codebase.

    Everything else takes `now` as an argument, which is what makes the seed reproducible and
    the DST tests possible.
    """
    return datetime.now(UTC)


def get_tz(name: str | None) -> ZoneInfo:
    return ZoneInfo(name) if name else DEFAULT_TZ


def to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("refusing to localise a naive datetime; all datetimes must be aware")
    return dt.astimezone(tz)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("refusing to convert a naive datetime; all datetimes must be aware")
    return dt.astimezone(UTC)


def local_naive_to_utc(d: date, t: time, tz: ZoneInfo) -> datetime:
    """Wall-clock local time -> the UTC instant we store.

    This is the (a) boundary: "Monday 08:00 at this clinic" becomes an absolute instant. Build
    slot grids by iterating over local *dates* and calling this, never by adding
    `timedelta(days=1)` to a UTC datetime — across a DST transition the latter drifts an hour
    and a Monday 08:00 slot silently becomes 07:00 or 09:00 local.
    """
    return datetime.combine(d, t).replace(tzinfo=tz).astimezone(UTC)


def local_day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of a local calendar day, in UTC. DST-correct: some days are 23 or 25 h."""
    start = local_naive_to_utc(d, time(0, 0), tz)
    end = local_naive_to_utc(d + timedelta(days=1), time(0, 0), tz)
    return start, end


def iso_week_start(d: date) -> date:
    """Monday of `d`'s ISO week."""
    return d - timedelta(days=d.weekday())


def business_days(start: date, weeks: int) -> Iterator[date]:
    """Mon–Fri dates for `weeks` ISO weeks beginning with `start`'s week."""
    monday = iso_week_start(start)
    for week in range(weeks):
        for offset in range(5):  # Mon..Fri
            yield monday + timedelta(days=week * 7 + offset)


def is_wall_clock_unambiguous(d: date, t: time, tz: ZoneInfo) -> bool:
    """False if this local wall-clock time is skipped or repeated by a DST transition.

    Sweden's transitions happen 02:00–03:00 on a Sunday, so Mon–Fri 08:00–17:00 clinic hours
    can never hit one. This function exists to *document that invariant* and to fire loudly
    the day somebody adds Saturday hours or an early-morning reminder job.
    """
    naive = datetime.combine(d, t)
    a = naive.replace(tzinfo=tz, fold=0)
    b = naive.replace(tzinfo=tz, fold=1)
    if a.utcoffset() != b.utcoffset():
        return False  # repeated hour (autumn)
    # Skipped hour (spring): the instant does not round-trip back to the same wall clock.
    return a.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive


def assert_wall_clock_unambiguous(d: date, t: time, tz: ZoneInfo) -> None:
    if not is_wall_clock_unambiguous(d, t, tz):
        raise ValueError(f"{d} {t} is ambiguous or nonexistent in {tz.key} (DST transition)")


# --- Swedish speech -------------------------------------------------------------------------
# Everything the agent says about a time goes through here. Spec §4: "availability returnerar
# färdigformulerad speech så LLM:en slipper formatera tider."


def _clock_sv(local: datetime) -> str:
    # "kl 10" reads better than "kl 10:00" when spoken; keep minutes only when they exist.
    if local.minute == 0:
        return f"kl {local.hour}"
    return f"kl {local.hour}:{local.minute:02d}"


def speak_date_sv(dt: datetime, tz: ZoneInfo) -> str:
    """'torsdag 20 augusti'."""
    local = to_local(dt, tz)
    return f"{WEEKDAYS_SV[local.weekday()]} {local.day} {MONTHS_SV[local.month - 1]}"


def speak_time_sv(dt: datetime, tz: ZoneInfo, *, now: datetime) -> str:
    """'imorgon kl 10', 'idag kl 14:30', 'på torsdag kl 9', 'torsdag 27 augusti kl 9'.

    Relative phrasing inside the next week is what makes the agent sound like a receptionist
    rather than a calendar. Beyond that, fall back to an explicit date so nobody mishears
    which Thursday is meant.
    """
    local = to_local(dt, tz)
    today = to_local(now, tz).date()
    delta = (local.date() - today).days

    if delta == 0:
        prefix = "idag"
    elif delta == 1:
        prefix = "imorgon"
    elif delta == 2:
        prefix = "i övermorgon"
    elif 3 <= delta <= 6:
        prefix = f"på {WEEKDAYS_SV[local.weekday()]}"
    else:
        prefix = speak_date_sv(dt, tz)

    return f"{prefix} {_clock_sv(local)}"
