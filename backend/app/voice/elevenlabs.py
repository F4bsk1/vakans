"""ElevenLabs Agents wiring — charter decision D1.

D1 originally defaulted to Retell. We switched to ElevenLabs because the charter had already
chosen ElevenLabs Flash v2.5 for TTS, so running the whole agent there removes a vendor hop
and one source of latency. The tradeoff is unchanged: a US-framed platform, fine for a demo
with fabricated patients, **not** fine once real patient data is on the line. `voice/adapter.py`
exists so that swap stays cheap.

**We generate; their CLI deploys.** This module turns a reviewed `clinic_profile` into the file
layout `@elevenlabs/cli` reads, and stops there. An earlier version also POSTed the config
itself — ninety-seven lines duplicating `elevenlabs agents push`, which would have needed
maintaining against their API forever. Deleted. Their CLI takes our files via `--from-file` and
`--config-path`, and owns auth, versioning and branches.

What is *not* duplicated is the generation. Their CLI can deploy a config; it cannot invent one
from a scraped clinic website. That part is the product:

    clinic website -> ingestion -> clinic_profile -> prompt + 7 tools -> elevenlabs/*.json

**What we emit and what we leave alone.** Everything that *follows from the clinic's data or
from Swedish*: the prompt, the tools, the ASR keyword list, the backchannel terms that must not
count as interruptions, the filler line while a tool runs, and number normalisation. Not
`voice_id` — which voice a clinic wants is decided by listening, and a deploy must never
overwrite an ear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

AGENT_TOKEN_HEADER = "X-Agent-Token"


@dataclass
class BodyParam:
    name: str
    type: str
    description: str
    required: bool = False


@dataclass
class WebhookTool:
    """One webhook tool, in readable form. `tool_payload` turns it into ElevenLabs' JSON."""

    name: str
    description: str
    method: str
    url: str
    body_params: list[BodyParam] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


def _tools(base_url: str, clinic_id: UUID, token_ref: str) -> list[WebhookTool]:
    root = f"{base_url.rstrip('/')}/agent/{clinic_id}"
    headers = {AGENT_TOKEN_HEADER: token_ref, "Content-Type": "application/json"}

    def tool(name: str, description: str, path: str, params: list[BodyParam]) -> WebhookTool:
        return WebhookTool(name, description, "POST", f"{root}/{path}", params, dict(headers))

    # Descriptions say what a tool *is* and when it applies. They used to shout instructions
    # ("Anropa ALLTID detta innan du nämner en tid"), which is how the agent ended up looping:
    # an unconditional trigger on a tool that returns the same answer every time is a loop.
    # Parameter lists stay short on purpose (spec §4: "Håll parametrarna få").
    return [
        tool(
            "availability",
            "Lediga tider hos kliniken. Svaret innehåller färdig text i speech och ett slot_id "
            "per ledig tid. När kunden valt en av tiderna använder du book.",
            "availability",
            [
                BodyParam("service_name", "string", "Behandlingen kunden bad om, som de sa det."),
                BodyParam("resource_name", "string", "Behandlare kunden bad om, om någon."),
            ],
        ),
        tool(
            "book",
            "Bokar tiden kunden valt. slot_id och service_id är värdena availability gav dig.",
            "book",
            [
                BodyParam("slot_id", "string", "slot_id från availability.", required=True),
                BodyParam("service_id", "string", "service_id från availability.", required=True),
                BodyParam("patient_name", "string", "Kundens namn.", required=True),
                BodyParam("phone", "string", "Kundens telefonnummer.", required=True),
            ],
        ),
        tool(
            "reschedule",
            "Flyttar en befintlig bokning till en ny tid. Kräver namn och telefonnummer.",
            "reschedule",
            [
                BodyParam("new_slot_id", "string", "Den nya tidens slot_id.", required=True),
                BodyParam("booking_ref", "string", "Bokningsreferens om kunden har den."),
                BodyParam("phone", "string", "Telefonnumret bokningen gjordes med.", required=True),
                BodyParam("name", "string", "Kundens namn."),
            ],
        ),
        tool(
            "cancel",
            "Avbokar en tid. Kräver namn och det telefonnummer bokningen gjordes med.",
            "cancel",
            [
                BodyParam("booking_ref", "string", "Bokningsreferens om kunden har den."),
                BodyParam("phone", "string", "Telefonnumret bokningen gjordes med.", required=True),
                BodyParam("name", "string", "Kundens namn."),
            ],
        ),
        tool(
            "waitlist",
            "Sätter upp kunden på väntelistan när ingen ledig tid passar. service_id kommer "
            "från availability-svaret.",
            "waitlist",
            [
                BodyParam("service_id", "string", "service_id från availability.", required=True),
                BodyParam("patient_name", "string", "Kundens namn.", required=True),
                BodyParam("phone", "string", "Kundens telefonnummer.", required=True),
            ],
        ),
        tool(
            "knowledge",
            "Praktiska frågor om kliniken: priser, öppettider, parkering, avbokningsregler, "
            "försäkring, remiss. Svaret är färdigformulerat — läs upp det i stället för att "
            "svara ur minnet.",
            "knowledge",
            [BodyParam("question", "string", "Kundens fråga, ordagrant.", required=True)],
        ),
        tool(
            "escalate",
            "Lämnar över till en människa. Använd vid medicinska frågor eller symtom "
            "(bröstsmärta, andnöd, svimning, blödning), vid klagomål, och när kunden ber om "
            "att få prata med en person. Läs upp svarets speech ordagrant.",
            "escalate",
            [
                BodyParam("reason", "string", "Vad kunden sa, ordagrant.", required=True),
                BodyParam("phone", "string", "Kundens telefonnummer om det är känt."),
                BodyParam("name", "string", "Kundens namn om det är känt."),
            ],
        ),
    ]


# --- the shapes ElevenLabs accepts ----------------------------------------------------------


# How long their side waits for one of our tools. Ours answer in well under 100 ms locally, but
# the request crosses a tunnel; their own template uses 30 and there is no reason to be tighter.
TOOL_TIMEOUT_SECONDS = 30

# Charter D1 picked Flash v2.5 for TTS before anything else was decided. It is also the only
# sensible option: the API refuses a Swedish agent on anything but flash/turbo v2.5, and turbo
# v2.5 is deprecated in favour of it.
TTS_MODEL_ID = "eleven_flash_v2_5"


def tool_payload(tool: WebhookTool) -> dict[str, Any]:
    """One tool, in the shape `elevenlabs tools push` reads from a `tool_configs/*.json` file.

    **Unwrapped.** The REST API takes `{"tool_config": {...}}`, and an earlier version of this
    function emitted that — which was wrong for the file, because their CLI reads the file and
    wraps it itself. Confirmed by reading `dist/tools/commands/impl.js` in their package rather
    than by guessing: `addTool` writes exactly these keys at the top level.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "type": "webhook",
        "api_schema": {
            "url": tool.url,
            "method": tool.method,
            "request_headers": dict(tool.headers),
            "request_body_schema": {
                "type": "object",
                "description": f"Request body for {tool.name}",
                "properties": {
                    p.name: {"type": p.type, "description": p.description} for p in tool.body_params
                },
                "required": [p.name for p in tool.body_params if p.required],
            },
        },
        "response_timeout_secs": TOOL_TIMEOUT_SECONDS,
    }


def tools_registry(
    *, slug: str, tool_names: list[str], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`tools.json` — the registry `elevenlabs tools push` reads.

    **Merges rather than overwrites.** `tools push` writes the id it gets back into this file,
    and an entry without an id is a *create*. So regenerating this blindly on every `make voice`
    — which happens every time the tunnel URL changes — would mint seven fresh tools each run
    and orphan the previous seven. Existing ids are matched by config path and carried over.
    """
    known: dict[str, str] = {
        entry["config"]: entry["id"]
        for entry in (existing or {}).get("tools", [])
        if entry.get("config") and entry.get("id")
    }

    tools: list[dict[str, Any]] = []
    for name in tool_names:
        config = f"tool_configs/{slug}-{name}.json"
        entry: dict[str, Any] = {"type": "webhook", "config": config}
        if config in known:
            entry["id"] = known[config]
        tools.append(entry)
    return {"tools": tools}


def tool_ids_from_registry(registry: dict[str, Any] | None) -> list[str]:
    """The ids `tools push` has recorded, for attaching to the agent."""
    return [e["id"] for e in (registry or {}).get("tools", []) if e.get("id")]


def tool_payloads(
    *, base_url: str, clinic_id: UUID, token_ref: str = "{{secret__agent_token}}"
) -> list[dict[str, Any]]:
    """All seven tools.

    `token_ref` defaults to a workspace-secret placeholder rather than the literal token: store
    the value once in ElevenLabs and reference it, so the shared secret is not copied into seven
    tool configs (or into a file that reaches git).
    """
    return [tool_payload(t) for t in _tools(base_url, clinic_id, token_ref)]


# Swedish backchannel. Swedes say these constantly *while listening*, and each one currently
# stops the agent mid-sentence. `interruption_ignore_terms` uses case-insensitive exact matching,
# so these only suppress a turn when the whole utterance is one of them — a caller who actually
# wants to interrupt says more than "mm".
SWEDISH_BACKCHANNEL: tuple[str, ...] = (
    "mm",
    "mhm",
    "hmm",
    "ja",
    "jaa",
    "javisst",
    "jaha",
    "jajj",
    "okej",
    "ok",
    "precis",
    "absolut",
    "visst",
    "aha",
    "just det",
    "tack",
    "toppen",
)

# Said while a tool call is in flight. Silence reads as "it broke"; "ett ögonblick" reads as a
# receptionist looking something up. This buys perceived latency without being faster.
SOFT_TIMEOUT_SECONDS = 1.6
FILLER_MESSAGES: tuple[str, ...] = (
    "Ett ögonblick, jag kollar.",
    "Jag tittar efter.",
    "Ska bara se här.",
)

# If the primary model stalls, cascade to a faster one rather than leaving dead air. Four
# seconds is their default and is a very long silence on a phone call; three is the compromise
# against their two-second floor. The fallbacks are chosen for latency, not for reasoning — by
# the time we are cascading, an answer beats a better answer.
BACKUP_LLM_ORDER: tuple[str, ...] = ("claude-haiku-4-5", "gemini-2.5-flash")
CASCADE_TIMEOUT_SECONDS = 3.0

# Words worth boosting in speech-to-text. Stop words would dilute the boost, so they are cut.
_KEYWORD_STOPWORDS = frozenset(
    ("och", "eller", "för", "med", "utan", "per", "av", "en", "ett", "den", "det", "min", "max")
)
MAX_ASR_KEYWORDS = 100


def asr_keywords(
    *, clinic_name: str, service_names: list[str], staff_names: list[str]
) -> list[str]:
    """Vocabulary to bias the Swedish transcriber towards.

    The highest-value setting on the platform for this product, and the cheapest: every
    mis-transcribed "naprapat" is a failed call, and the fix is a list of words we already have
    in the reviewed clinic profile.

    Practitioner names go in whole *and* split, because a caller says "Johanna" far more often
    than "Johanna Lindqvist". Service names are split into their distinctive words for the same
    reason — nobody asks for a "Klassisk massage 60 min", they ask for a massage.
    """
    words: list[str] = []
    for name in [clinic_name, *staff_names]:
        words.append(name)
        words.extend(name.split())
    for service in service_names:
        words.extend(service.split())

    seen: dict[str, None] = {}
    for word in words:
        cleaned = word.strip(" .,–-").strip()
        if len(cleaned) < 3 or cleaned.lower() in _KEYWORD_STOPWORDS or cleaned.isdigit():
            continue
        seen.setdefault(cleaned, None)
    return list(seen)[:MAX_ASR_KEYWORDS]


def agent_config(
    *,
    clinic_name: str,
    prompt: str,
    first_message: str,
    llm: str,
    keywords: list[str],
    tool_ids: list[str],
    language: str = "sv",
) -> dict[str, Any]:
    """The contents of `agent_configs/<slug>.json`.

    We generate every setting that *follows from the clinic's data or from Swedish*, and none of
    the settings that are a matter of taste. Concretely: `voice_id` is absent, because which
    voice a clinic wants is decided by listening and a deploy must never overwrite that. The
    ASR keywords, the backchannel list and the filler messages are not taste — they are the same
    for any Swedish clinic, or derived from this one's own profile.
    """
    # Never list the primary model as its own fallback.
    fallbacks = [m for m in BACKUP_LLM_ORDER if m != llm]

    return {
        "name": clinic_name,
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": prompt,
                    "llm": llm,
                    # Empty until `elevenlabs tools push` has run and recorded the ids in
                    # tools.json. Push the tools first, regenerate, then push the agent.
                    "tool_ids": tool_ids,
                    "backup_llm_config": {"preference": "override", "order": fallbacks}
                    if fallbacks
                    else {"preference": "default"},
                    "cascade_timeout_seconds": CASCADE_TIMEOUT_SECONDS,
                },
                "first_message": first_message,
                "language": language,
                # The opening line is the EU AI Act art. 50 disclosure. Left interruptible, an
                # eager caller talks over it and it legally did not happen — so this is a legal
                # requirement expressed as a config flag, not a politeness setting.
                "disable_first_message_interruptions": True,
            },
            "asr": {"keywords": keywords},
            "turn": {
                "interruption_ignore_terms": list(SWEDISH_BACKCHANNEL),
                # Their curated defaults on top of ours, rather than instead of.
                "merge_with_default_ignore_terms": True,
                "soft_timeout_config": {
                    "timeout_seconds": SOFT_TIMEOUT_SECONDS,
                    "message": FILLER_MESSAGES[0],
                    "additional_soft_timeout_messages": list(FILLER_MESSAGES[1:]),
                    "randomize_fillers": True,
                    # One filler per response. Stacking them sounds like stalling.
                    "max_soft_timeouts_per_generation": 1,
                    # Never fill during the greeting — the AI disclosure must land cleanly.
                    "disable_until_first_user_message": True,
                },
            },
            "tts": {
                # Required, not optional: ElevenLabs rejects a non-English agent that does not
                # use flash or turbo v2.5 ("Non-english Agents must use turbo or flash v2_5"),
                # and the default is an English-only model. Flash v2.5 is also the charter's
                # choice — ~75 ms, 32 languages including Swedish, and turbo v2.5 is deprecated.
                "model_id": TTS_MODEL_ID,
                # "elevenlabs", not the default "system_prompt". The default asks the *model* to
                # rewrite numbers as words before speaking, which means it edits our prices on
                # the way out — exactly the thing every other decision here prevents. This one
                # normalises after the model, so "895 kronor" is still the number we sent.
                "text_normalisation_type": "elevenlabs",
            },
        },
    }


def agents_registry(
    *, slug: str, agent_id: str | None, existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """`agents.json` — the registry `elevenlabs agents push` reads.

    Same merge rule as `tools_registry`: `agents add` writes the new agent's id in here, and
    losing it on the next regenerate would create a second agent instead of updating the first.
    An id already in the file wins over the one in `.env`, because the file is what the CLI
    actually wrote.
    """
    config = f"agent_configs/{slug}.json"
    known = next(
        (e.get("id") for e in (existing or {}).get("agents", []) if e.get("config") == config),
        None,
    )
    entry: dict[str, Any] = {"config": config}
    if known or agent_id:
        entry["id"] = known or agent_id
    return {"agents": [entry]}


# --- the human setup sheet -------------------------------------------------------------------


def as_markdown(*, base_url: str, clinic_id: UUID, clinic_name: str) -> str:
    """A followable sheet for entering the tools by hand, when a push is not an option."""
    lines = [
        f"# ElevenLabs webhook tools — {clinic_name}",
        "",
        f"Base URL: `{base_url}`",
        f"clinic_id: `{clinic_id}`",
        "",
        "> Prefer `vakans voice --slug <clinic> --push`. This sheet is the fallback.",
        "",
        "## Before you start",
        "",
        "1. Store the agent token as a **workspace secret** named `agent_token` "
        "(value: `VAKANS_AGENT_TOKEN` from your .env). Do not paste it into each tool.",
        f"2. Every tool below needs the header `{AGENT_TOKEN_HEADER}` referencing that secret.",
        "3. All seven are POST and return JSON with a `speech` field — the agent reads that "
        "field aloud verbatim.",
        "",
    ]
    for t in _tools(base_url, clinic_id, "{{secret__agent_token}}"):
        lines += [
            f"## {t.name}",
            "",
            f"- **Method**: {t.method}",
            f"- **URL**: `{t.url}`",
            f"- **Description**: {t.description}",
            "- **Body parameters**:",
        ]
        lines += [
            f"  - `{p.name}` ({p.type}{', required' if p.required else ''}) — {p.description}"
            for p in t.body_params
        ] or ["  - (none)"]
        lines.append("")
    return "\n".join(lines)


def as_json(*, base_url: str, clinic_id: UUID) -> str:
    return json.dumps(
        tool_payloads(base_url=base_url, clinic_id=clinic_id), ensure_ascii=False, indent=2
    )
