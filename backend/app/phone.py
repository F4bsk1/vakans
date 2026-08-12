"""Phone normalisation and the send-guard.

`may_send_sms` is the single most safety-critical function in the demo. Accidentally texting a
real stranger from a *healthcare* product is a company-ending category of mistake, and the
demo database is deliberately full of plausible-looking Swedish mobile numbers.

There is no SMS code yet — that is step 3. This exists now, with tests, so that step 3 has no
excuse to invent its own check.
"""

from __future__ import annotations

import re

SE_COUNTRY_CODE = "46"
_SEPARATORS = re.compile(r"[\s\-()./]")


def normalize_e164(raw: str | None, *, default_country: str = SE_COUNTRY_CODE) -> str | None:
    """Normalise the Swedish formats people actually write into E.164.

    Handles `070-123 45 67`, `+46 70 123 45 67`, `0046 70 1234567`, `(070) 123 45 67`.
    Returns None for anything that does not look like a phone number, rather than guessing.
    """
    if not raw:
        return None

    cleaned = _SEPARATORS.sub("", raw.strip())
    if not cleaned:
        return None

    if cleaned.startswith("+"):
        digits, prefix = cleaned[1:], "+"
    elif cleaned.startswith("00"):
        digits, prefix = cleaned[2:], "+"
    elif cleaned.startswith("0"):
        # National format: drop the trunk 0 and prepend the country code.
        digits, prefix = default_country + cleaned[1:], "+"
    else:
        digits, prefix = cleaned, "+"

    if not digits.isdigit():
        return None
    # E.164 allows at most 15 digits; anything under 8 is not a reachable number.
    if not (8 <= len(digits) <= 15):
        return None

    return prefix + digits


def same_number(a: str | None, b: str | None) -> bool:
    na, nb = normalize_e164(a), normalize_e164(b)
    return na is not None and na == nb


def is_swedish_mobile(phone: str | None) -> bool:
    n = normalize_e164(phone)
    return bool(n and n.startswith("+467"))


def may_send_sms(phone: str | None, *, sms_provider: str, founder_mobile: str | None) -> bool:
    """Whether we are allowed to actually send an SMS to this number.

    Exactly two cases are permitted:

    1. The provider is `fake`, so nothing leaves the building.
    2. The number is the founder's own mobile — the demo's "JA-svararen", the one real phone
       that is supposed to receive a real text.

    Every other number in the demo database is fabricated, and a fabricated Swedish mobile
    number is very likely to belong to a real person. Sweden has no reserved test range, so
    this predicate is the only thing standing between the demo and a stranger's phone.
    """
    if sms_provider == "fake":
        return True
    if not phone:
        return False
    return same_number(phone, founder_mobile)
