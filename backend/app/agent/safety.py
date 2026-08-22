"""The medical boundary — charter §2, spec §4 rule 3.

The charter is explicit that this is "a product line, not a nice-to-have". The system prompt
tells the agent to call `escalate` when it hears a red flag, and it usually will — but "usually"
is the wrong standard when the failure mode is a caller with chest pain being offered a
physiotherapy appointment. So this runs on the server too.

**Be precise about what that does and does not cover.** We are the *server*: ElevenLabs holds
the microphone and the transcript, and calls us. So the only text we ever see is the arguments
the model chose to put in a tool call — never the caller's actual words. Concretely:

- `knowledge(question=...)` and `escalate(reason=...)` are documented as "ordagrant", so they
  usually do carry what the caller said. This is where the check earns its keep: a caller who
  asks "är det farligt att det gör ont i bröstet?" gets stopped here even when the model routed
  it as an ordinary question.
- `availability(service_name=...)` receives the model's *interpretation* — "massage", not the
  sentence around it. A red flag phrased there will usually not reach us at all.

So this is a **backstop on the fields we receive, not a filter on the conversation**. The
real-time boundary is the model's, and the prompt has to carry it. ElevenLabs offers no
real-time transcript; their post-call webhook delivers the full transcript after the call ends,
which is the right place to *audit* how often the boundary held — see docs/RUNBOOK.md.

Two tiers, matching spec §4:

1. `EMERGENCY` — bröstsmärta, andnöd, svimning, kraftig blödning and similar. Refer to 112/1177
   and offer a human callback.
2. `ADVICE` — any request to assess a symptom or recommend treatment. Decline and refer to 1177.

Both are returned to the caller as a fixed `speech` string. The agent reads it verbatim; there
is no path where a model gets to improvise a response to a red flag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class RedFlag(StrEnum):
    EMERGENCY = "emergency"
    ADVICE = "advice"


# Spec §4 names bröstsmärta, andnöd, svimning and kraftig blödning explicitly. The rest are
# the obvious neighbours — stroke signs, unconsciousness, severe allergic reaction — plus the
# inflections and colloquialisms a real caller uses. Deliberately over-inclusive: a false
# positive costs one unnecessary "ring 1177"; a false negative is a headline.
EMERGENCY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("bröstsmärta", r"bröstsmärt|ont i bröstet|tryck över bröstet|smärta i bröstet"),
    ("andnöd", r"andnöd|andas inte|svårt att andas|får inte luft|kan inte andas|kippar efter"),
    ("svimning", r"svimma|svimning|svimmat|svimmade|medvetslös|blackout|tappat medvetandet"),
    ("blödning", r"kraftig blödning|blöder kraftigt|blöder mycket|stor blödning|blodförlust"),
    ("stroke", r"ansiktet hänger|släpar på orden|talsvårigheter|förlamn|domningar i ena|stroke"),
    ("hjärtinfarkt", r"hjärtinfarkt|infarkt|hjärtstopp"),
    ("allergi", r"anafylak|allergisk chock|svullen i halsen|svullnar i halsen"),
    ("suicid", r"ta mitt liv|ta sitt liv|självmord|vill inte leva|göra mig själv illa"),
    ("akut", r"\bakut(a|t)? (smärta|tillstånd)|livshotande|ambulans"),
)

# Requests for clinical judgement. Not an emergency, but squarely over the line: the agent
# never assesses symptoms or recommends treatment (charter §2).
ADVICE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("symtombedömning", r"vad (tror|tror du|kan det vara|är det för fel|beror det på)"),
    ("symtombedömning", r"är det (farligt|allvarligt|normalt|nåt allvarligt|något allvarligt)"),
    ("symtombedömning", r"(bör|ska|borde) jag (oroa|vara orolig|söka vård|göra något)"),
    ("diagnos", r"har jag (fått|drabbats av|en |ett )|vad har jag för|kan det vara (en|ett) "),
    ("behandlingsråd", r"vilken behandling|hur (ska|bör) jag behandla|vad ska jag göra åt"),
    ("läkemedel", r"medicin|läkemedel|tabletter|recept|antiinflammatoriskt|ipren|alvedon|dosering"),
    ("träningsråd", r"(får|kan|bör) jag (träna|springa|lyfta|belasta)"),
)

_EMERGENCY = tuple((label, re.compile(p, re.IGNORECASE)) for label, p in EMERGENCY_PATTERNS)
_ADVICE = tuple((label, re.compile(p, re.IGNORECASE)) for label, p in ADVICE_PATTERNS)

# Read verbatim by the agent. Spec §4 rule 3 gives the wording; do not paraphrase it, and do
# not make it clinic-configurable.
EMERGENCY_SPEECH = (
    "Det låter som något du ska söka vård för direkt – ring 112 om det är akut, "
    "annars 1177. Vill du att en kollega ringer upp dig?"
)
ADVICE_SPEECH = (
    "Det kan jag tyvärr inte bedöma – för rådgivning ring 1177. "
    "Men jag hjälper gärna till att boka en tid."
)
CALLBACK_SPEECH = "Jag har noterat det och ber en kollega ringa upp dig så snart som möjligt."


@dataclass(frozen=True)
class SafetyVerdict:
    flag: RedFlag | None
    matched: tuple[str, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.flag is not None

    @property
    def speech(self) -> str:
        if self.flag is RedFlag.EMERGENCY:
            return EMERGENCY_SPEECH
        if self.flag is RedFlag.ADVICE:
            return ADVICE_SPEECH
        return ""


CLEAR = SafetyVerdict(flag=None)


def check(*texts: str | None) -> SafetyVerdict:
    """Screen any caller-supplied free text before acting on it.

    Emergency wins over advice: if a caller says "jag har ont i bröstet, vad tror du?", the
    answer is 112, not "I can't assess that".
    """
    haystack = " ".join(t for t in texts if t).lower()
    if not haystack.strip():
        return CLEAR

    emergency = tuple(label for label, pattern in _EMERGENCY if pattern.search(haystack))
    if emergency:
        return SafetyVerdict(RedFlag.EMERGENCY, tuple(dict.fromkeys(emergency)))

    advice = tuple(label for label, pattern in _ADVICE if pattern.search(haystack))
    if advice:
        return SafetyVerdict(RedFlag.ADVICE, tuple(dict.fromkeys(advice)))

    return CLEAR
