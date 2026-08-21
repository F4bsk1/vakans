"""ElevenLabs Agents wiring — charter decision D1.

D1 originally defaulted to Retell. We switched to ElevenLabs because the charter had already
chosen ElevenLabs Flash v2.5 for TTS, so running the whole agent there removes a vendor hop
and one source of latency. The tradeoff is unchanged: a US-framed platform, fine for a demo
with fabricated patients, **not** fine once real patient data is on the line. `voice/adapter.py`
exists so that swap stays cheap.

This module generates the seven webhook-tool definitions from a clinic's own data. They are
generated rather than hand-entered for the same reason the system prompt is: the product is
one agent per clinic, and clicking through a dashboard per clinic breaks the charter's
"<10 min manual work" promise.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
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
    """One ElevenLabs webhook tool.

    Field names follow their documented configuration (name, description, method, URL, body
    parameters, headers). Verify the exact JSON keys against the API before pushing
    programmatically — this shape is written for dashboard entry and review.
    """

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

    # Descriptions are prescriptive about *when* to call, not just what the tool does — that is
    # what actually drives a model to reach for it at the right moment. Parameter lists are
    # kept short on purpose (spec §4: "Håll parametrarna få — LLM:en klantar sig med många").
    return [
        tool(
            "availability",
            "Hämta lediga tider. Anropa EN gång, när kunden ber om en tid och du ännu inte "
            "har några. Anropa INTE igen när kunden valt en av tiderna — använd book då. "
            "Svaret innehåller färdig text i speech och ett slot_id per ledig tid.",
            "availability",
            [
                BodyParam("service_name", "string", "Behandlingen kunden bad om, som de sa det."),
                BodyParam("resource_name", "string", "Behandlare kunden bad om, om någon."),
            ],
        ),
        tool(
            "book",
            "Boka tiden kunden valt. Anropa så snart du har slot_id, service_id, namn och "
            "telefonnummer — vänta inte och fråga inte om lediga tider igen. slot_id och "
            "service_id är exakt de värden availability returnerade.",
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
            "Flytta en befintlig bokning till en ny tid. Be först om namn och telefonnummer.",
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
            "Avboka en tid. Be ALLTID om namn och det telefonnummer bokningen gjordes med "
            "innan du anropar detta.",
            "cancel",
            [
                BodyParam("booking_ref", "string", "Bokningsreferens om kunden har den."),
                BodyParam("phone", "string", "Telefonnumret bokningen gjordes med.", required=True),
                BodyParam("name", "string", "Kundens namn."),
            ],
        ),
        tool(
            "waitlist",
            "Sätt upp kunden på väntelistan när ingen tid passar. service_id kommer från "
            "availability-svaret.",
            "waitlist",
            [
                BodyParam("service_id", "string", "service_id från availability.", required=True),
                BodyParam("patient_name", "string", "Kundens namn.", required=True),
                BodyParam("phone", "string", "Kundens telefonnummer.", required=True),
            ],
        ),
        tool(
            "knowledge",
            "Svara på praktiska frågor om kliniken: priser, öppettider, parkering, avbokning, "
            "försäkring, remiss. Anropa detta i stället för att svara ur minnet — särskilt om "
            "priser.",
            "knowledge",
            [BodyParam("question", "string", "Kundens fråga, ordagrant.", required=True)],
        ),
        tool(
            "escalate",
            "Anropa OMEDELBART vid medicinska frågor eller symtom (bröstsmärta, andnöd, "
            "svimning, blödning), vid klagomål, eller när kunden vill prata med en människa. "
            "Läs upp svarets speech-fält ordagrant.",
            "escalate",
            [
                BodyParam("reason", "string", "Vad kunden sa, ordagrant.", required=True),
                BodyParam("phone", "string", "Kundens telefonnummer om det är känt."),
                BodyParam("name", "string", "Kundens namn om det är känt."),
            ],
        ),
    ]


def tool_definitions(
    *, base_url: str, clinic_id: UUID, token_ref: str = "{{secret__agent_token}}"
) -> list[dict[str, Any]]:
    """Webhook tool definitions for one clinic.

    `token_ref` defaults to a secret placeholder rather than the literal token: store the
    value once as a workspace secret in ElevenLabs and reference it, so the shared secret is
    not pasted into seven separate tool configs (or into a file that reaches git).
    """
    return [asdict(t) for t in _tools(base_url, clinic_id, token_ref)]


def as_markdown(*, base_url: str, clinic_id: UUID, clinic_name: str) -> str:
    """A human-followable setup sheet for entering the tools in the dashboard."""
    lines = [
        f"# ElevenLabs webhook tools — {clinic_name}",
        "",
        f"Base URL: `{base_url}`",
        f"clinic_id: `{clinic_id}`",
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
        tool_definitions(base_url=base_url, clinic_id=clinic_id), ensure_ascii=False, indent=2
    )
