"""Human-readable booking references (DELTA G2).

These are spoken and heard over a phone line, in Swedish, through a TTS voice and back through
STT. That constrains the alphabet more than a normal short-code would:

- No 0/O and no 1/I/L — the classic pairs that STT mangles.
- No U/V: in spoken Swedish "u" and "v" are easy to confuse over a compressed phone codec.

Six characters from the remaining 30 gives ~7.3e8 combinations, which is far more than a demo
clinic needs, and collisions are caught by the (clinic_id, booking_ref) unique constraint.
"""

from __future__ import annotations

import random

ALPHABET = "23456789ABCDEFGHJKMNPQRSTWXYZ"
REF_LENGTH = 6

# What callers and STT actually produce for characters that are not in our alphabet.
_CONFUSIONS = str.maketrans({"O": "0", "I": "1", "L": "1", "U": "V"})


def new_booking_ref(rng: random.Random, *, length: int = REF_LENGTH) -> str:
    """Generate a reference. Takes an explicit Random so the seed generator stays deterministic."""
    return "".join(rng.choice(ALPHABET) for _ in range(length))


def normalize_booking_ref(raw: str) -> str:
    """Normalise what a caller said or typed into our alphabet.

    Strips separators and upper-cases, then maps out characters our alphabet deliberately
    excludes so that a caller reading "oh" for zero still resolves.
    """
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    cleaned = cleaned.translate(_CONFUSIONS)
    # 0 and 1 are not in the alphabet either; they are what the confusions map *to*, so map
    # them onto the nearest members our generator can actually emit.
    return cleaned.replace("0", "Q").replace("1", "7")


def is_valid_booking_ref(ref: str) -> bool:
    return len(ref) == REF_LENGTH and all(ch in ALPHABET for ch in ref)
