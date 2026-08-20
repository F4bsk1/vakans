"""Swedish phrasing for everything the agent says.

The charter's latency trick: agent tools return a ready-to-read `speech` string so the LLM
never has to render a Swedish date or a price. That keeps turns under the 1.5 s target and
removes a whole class of failure — a model that formats "895,00 kr" as "eight hundred
ninety-five" or invents "kvart över tio".

Everything here is deterministic and unit-tested. `app.timeutils.speak_time_sv` remains the
only place a clock time becomes Swedish words.
"""

from __future__ import annotations

from decimal import Decimal
from zoneinfo import ZoneInfo

from app.timeutils import speak_time_sv


# A practitioner is introduced by first name only — "hos Johanna", not "hos Johanna Lindqvist".
# That is how a receptionist actually speaks, and it keeps the sentence short.
def first_name(full_name: str) -> str:
    return full_name.strip().split()[0] if full_name.strip() else full_name


def speak_price(price: Decimal | float | None) -> str:
    """'895 kronor'. Never invents a number — spec §4 rule 5."""
    if price is None:
        return "Priset kan jag tyvärr inte se här, men en kollega kan svara på det"
    value = Decimal(str(price))
    whole = value.quantize(Decimal("1"))
    if value == whole:
        return f"{int(whole)} kronor"
    return f"{value.normalize()} kronor".replace(".", " kronor och ").replace(",", ".")


def speak_duration(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        hours = minutes // 60
        return "en timme" if hours == 1 else f"{hours} timmar"
    return f"{minutes} minuter"


def join_sv(parts: list[str]) -> str:
    """'a, b eller c' — Swedish list conjunction for reading aloud."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} eller {parts[-1]}"


def speak_slot(starts_at, resource_name: str, tz: ZoneInfo, *, now) -> str:
    """'imorgon kl 10 hos Johanna' — spec §3's worked example."""
    when = speak_time_sv(starts_at, tz, now=now)
    return f"{when} hos {first_name(resource_name)}"


def speak_availability(options: list[tuple], tz: ZoneInfo, *, now, service_name: str | None) -> str:
    """Offer up to three times. Spec §4 rule 1: never read more than 3 alternatives."""
    if not options:
        base = "Jag hittar tyvärr inga lediga tider"
        if service_name:
            base += f" för {service_name}"
        return base + " just nu. Vill du att jag sätter upp dig på väntelistan?"

    spoken = [speak_slot(starts_at, name, tz, now=now) for starts_at, name in options[:3]]
    lead = f"Jag har {service_name} " if service_name else "Jag har "
    if len(spoken) == 1:
        return f"{lead}{spoken[0]}. Passar det?"
    return f"{lead}{join_sv(spoken)}. Vilken passar bäst?"


def speak_booking_confirmation(
    *,
    service_name: str,
    starts_at,
    resource_name: str,
    price: Decimal | float | None,
    booking_ref: str,
    tz: ZoneInfo,
    now,
) -> str:
    """Spec §4 rule 6: summarise service, time, practitioner and price before confirming."""
    when = speak_time_sv(starts_at, tz, now=now)
    parts = [f"Då har jag bokat {service_name} {when} hos {first_name(resource_name)}"]
    if price is not None:
        parts.append(f"Det kostar {speak_price(price)}")
    parts.append(f"Din bokningsreferens är {spell_ref(booking_ref)}")
    parts.append("Du får en bekräftelse via SMS")
    return ". ".join(parts) + "."


def spell_ref(ref: str) -> str:
    """Space out a booking reference so TTS reads it character by character.

    "A7K2M9" spoken as a word is unintelligible; spaced, it is dictation. The alphabet in
    app.booking_ref already excludes the characters Swedish STT confuses on the way back.
    """
    return " ".join(ref.upper())


def speak_cancellation(
    *, service_name: str, starts_at, tz: ZoneInfo, now, policy: str | None
) -> str:
    when = speak_time_sv(starts_at, tz, now=now)
    speech = f"Din tid för {service_name} {when} är avbokad."
    if policy:
        speech += " Vi hör av oss om något mer behövs."
    return speech


def speak_reschedule(*, service_name: str, starts_at, resource_name: str, tz: ZoneInfo, now) -> str:
    when = speak_time_sv(starts_at, tz, now=now)
    return (
        f"Klart – jag har flyttat din tid för {service_name} till {when} "
        f"hos {first_name(resource_name)}. Du får en ny bekräftelse via SMS."
    )


def speak_waitlisted(service_name: str) -> str:
    return (
        f"Jag har satt upp dig på väntelistan för {service_name}. "
        "Vi hör av oss så snart en tid blir ledig."
    )


NOT_FOUND_SPEECH = (
    "Jag hittar tyvärr ingen bokning på de uppgifterna. "
    "Kan du läsa upp namn och telefonnummer igen?"
)
SLOT_TAKEN_SPEECH = (
    "Tiden hann tyvärr bli bokad av någon annan. Vill du att jag letar upp en ny tid?"
)
UNKNOWN_SERVICE_SPEECH = "Jag är inte säker på vilken behandling du menar. Kan du säga om det?"
