"""Swedish SMS bodies for the refill cascade.

Separate from `app.agent.speech` on purpose: that module renders what a *voice* says, this one
renders what a *text message* says, and the two have different constraints. An SMS is read, not
heard, so it may contain a booking reference in plain form — but it is billed per 160 GSM-7
characters, so every body here is kept to one segment for a typical Swedish clinic name.

Nothing here is prompt-tunable and no LLM sees it. A cascade message that garbles a time or a
price is worse than one that sounds mechanical.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.agent.speech import first_name
from app.timeutils import speak_time_sv

# Spec §3 caps a cascade message at "Svara JA så är den din" plus the facts. Anything longer
# reads as marketing, and a patient who has to scroll does not reply within 90 seconds.


def window_sv(timeout: timedelta) -> str:
    """'90 sekunder' / '2 minuter' — derived from the real timeout, never hardcoded.

    Stating a window the engine does not actually honour is the fastest way to make a patient
    distrust the whole channel.
    """
    seconds = int(timeout.total_seconds())
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return "en minut" if minutes == 1 else f"{minutes} minuter"
    return f"{seconds} sekunder"


def offer_sms(
    *,
    clinic_name: str,
    patient_name: str | None,
    service_name: str,
    starts_at: datetime,
    resource_name: str,
    tz: ZoneInfo,
    now: datetime,
    timeout: timedelta,
) -> str:
    """Spec §3 step 3, verbatim in shape: greeting, what, when, and the one word to reply."""
    greeting = f"Hej {first_name(patient_name)}! " if patient_name else "Hej! "
    when = speak_time_sv(starts_at, tz, now=now)
    return (
        f"{greeting}{clinic_name}: en tid för {service_name} har blivit ledig "
        f"{when} hos {first_name(resource_name)}. "
        f"Svara JA så är den din (gäller {window_sv(timeout)})."
    )


def confirmation_sms(
    *,
    clinic_name: str,
    service_name: str,
    starts_at: datetime,
    resource_name: str,
    booking_ref: str,
    tz: ZoneInfo,
    now: datetime,
) -> str:
    return (
        f"{clinic_name}: klart! {service_name} {speak_time_sv(starts_at, tz, now=now)} "
        f"hos {first_name(resource_name)} är bokad. Ref {booking_ref}. "
        f"Avboka senast 24h innan."
    )


def too_late_sms(*, clinic_name: str) -> str:
    """Someone else took the slot between the offer and the reply. Say so plainly."""
    return (
        f"{clinic_name}: tyvärr, tiden hann bli tagen av någon annan. Du står kvar på väntelistan."
    )


def unclear_reply_sms(*, clinic_name: str) -> str:
    return f"{clinic_name}: svara JA om du vill ha tiden, annars behöver du inte göra något."


def declined_sms(*, clinic_name: str) -> str:
    return f"{clinic_name}: tack för svaret. Du står kvar på väntelistan till nästa gång."
