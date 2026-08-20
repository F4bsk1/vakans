"""Fuzzy matching of what the caller said to what the clinic has.

Everything reaching these functions has been through Swedish speech-to-text, so exact matches
are the exception. A caller says "fysio", "en tid hos Johanna", "vanlig massage" — never
"Nybesök fysioterapi". Matching too strictly means the agent says "I'm not sure which treatment
you mean" to a caller who was perfectly clear.

Deliberately lexical rather than embedding-based: it is deterministic, instant, testable, and
needs no model at request time — which matters when the budget is 1.5 seconds per turn.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

# Swedish filler that carries no signal for matching. Kept as prose a human can edit rather
# than sixty quoted strings.
_STOPWORD_TEXT = """
en ett den det de dem denna detta dessa och eller men som att av på i till för med om
har hade är var vara blir blev jag du vi ni han hon min din vår er hos vid från
kan ska skulle vill vilja gärna tack hej ja nej lite nån någon något
tid tider besök gången boka bokning
"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

# Common ways callers refer to services, mapped onto words that appear in real service names.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "fysio": ("fysioterapi", "sjukgymnastik", "fysioterapeut"),
    "sjukgymnast": ("fysioterapi", "sjukgymnastik"),
    "naprapat": ("naprapati",),
    "kiropraktor": ("kiropraktik",),
    "massage": ("massage", "massageterapi"),
    "första": ("nybesök",),
    "ny": ("nybesök",),
    "nytt": ("nybesök",),
    "uppföljning": ("återbesök",),
    "återkommande": ("återbesök",),
    "vanlig": ("klassisk",),
    "rygg": ("rygg", "naprapati"),
    "nacke": ("nacke", "naprapati"),
    "knä": ("knä", "fysioterapi"),
    "axel": ("axel", "skuldra"),
}


def normalize(text: str | None) -> str:
    if not text:
        return ""
    # Casefold handles Swedish correctly; strip only combining marks so å/ä/ö survive as
    # themselves rather than collapsing to a/a/o and colliding with unrelated words.
    cleaned = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"[^\w\såäöéèü-]", " ", cleaned).strip()


def tokenize(text: str | None) -> list[str]:
    tokens = [t for t in normalize(text).split() if t and t not in STOPWORDS]
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(_synonyms_for(token))
    return expanded


def token_groups(text: str | None) -> list[set[str]]:
    """One set per spoken word: the word plus everything it could mean."""
    return [
        {token, *_synonyms_for(token)}
        for token in normalize(text).split()
        if token and token not in STOPWORDS
    ]


# Swedish marks the definite article as a suffix, so callers say "sjukgymnasten" and
# "massagen" where the service list says "sjukgymnast" and "massage". Without this the
# synonym table silently misses the most natural way to ask.
_INFLECTIONS = ("erna", "arna", "en", "et", "er", "ar", "na", "n", "t")


def _synonyms_for(token: str) -> tuple[str, ...]:
    if (direct := SYNONYMS.get(token)) is not None:
        return direct
    for suffix in _INFLECTIONS:
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            stem = token[: -len(suffix)]
            if (found := SYNONYMS.get(stem)) is not None:
                return (stem, *found)
    return ()


@dataclass(frozen=True)
class Match[T]:
    item: T
    score: float

    @property
    def is_confident(self) -> bool:
        return self.score >= 0.5


def score_text(query: str | None, candidate: str | None) -> float:
    """0..1 overlap between a spoken phrase and a stored name.

    Scored against the *query's* token count, not the candidate's: a caller saying "massage"
    should match "Klassisk massage 60 min" strongly, even though most of that name went unsaid.
    """
    # One group per *spoken* token: the word itself plus its synonyms. Scoring per group
    # rather than per expanded token keeps the denominator honest — otherwise a word with
    # three synonyms is penalised for having them, and "hos sjukgymnasten" scores 0.25
    # against "Nybesök fysioterapi" instead of a clean hit.
    q_groups = token_groups(query)
    c_tokens = set(tokenize(candidate))
    if not q_groups or not c_tokens:
        return 0.0

    hits = 0.0
    for group in q_groups:
        if group & c_tokens:
            hits += 1.0
        elif any(_shares_a_stem(token, c_tokens) for token in group):
            hits += 0.6  # partial credit: "fysioterapeut" vs "fysioterapi"
    score = hits / len(q_groups)

    # A whole-phrase substring is a strong signal the token overlap can understate.
    if normalize(query) and normalize(query) in normalize(candidate):
        score = max(score, 0.9)
    return min(score, 1.0)


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


def _shares_a_stem(token: str, candidates: set[str], *, min_prefix: int = 5) -> bool:
    """Do these words share a Swedish stem?

    Prefix *containment* is too strict for the pairs that matter here: "fysioterapi" and
    "fysioterapeut" are plainly the same domain, but neither starts with the other — they
    diverge at the tenth character. Comparing the shared prefix instead catches that, and
    catches "naprapati"/"naprapat" and "massage"/"massageterapeut" too.

    The floor is deliberately high enough that "fysio" and "fysik" (four shared characters)
    do not match.
    """
    for candidate in candidates:
        shared = common_prefix_len(token, candidate)
        needed = max(min_prefix, 0.6 * min(len(token), len(candidate)))
        if shared >= needed:
            return True
    return False


def best_match[T](query: str | None, items: list[T], key: Callable[[T], str]) -> Match[T] | None:
    """Highest-scoring item, or None if nothing scored above zero."""
    if not query or not items:
        return None
    scored = [(score_text(query, key(item)), item) for item in items]
    score, item = max(scored, key=lambda pair: pair[0])
    return Match(item=item, score=score) if score > 0 else None
