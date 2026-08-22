"""ElevenLabs Agents wiring — charter decision D1.

D1 originally defaulted to Retell. We switched to ElevenLabs because the charter had already
chosen ElevenLabs Flash v2.5 for TTS, so running the whole agent there removes a vendor hop
and one source of latency. The tradeoff is unchanged: a US-framed platform, fine for a demo
with fabricated patients, **not** fine once real patient data is on the line. `voice/adapter.py`
exists so that swap stays cheap.

**Config as code.** This module is the only source of truth for what the agent is. It emits the
shapes ElevenLabs' own API and CLI accept, so the same generated files can be pushed either by
`vakans voice --push` (this file) or by `elevenlabs agents push` (their CLI). One definition,
two transports, no drift. The alternative — clicking through a dashboard once per clinic —
breaks the charter's "<10 min manual work" promise the moment there is a second clinic.

**What we own and what we do not.** A push writes only the prompt, the first message, the
language and the tool list. Voice, LLM choice and turn-taking stay whatever the dashboard says,
because those are listening decisions made by ear. `PATCH` merges, so not sending a field leaves
it alone — that is deliberate, not an oversight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from app.config import Settings

logger = logging.getLogger("vakans.voice")

AGENT_TOKEN_HEADER = "X-Agent-Token"
API_ROOT = "https://api.elevenlabs.io/v1/convai"
API_TIMEOUT_S = 30.0


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


def tool_payload(tool: WebhookTool) -> dict[str, Any]:
    """One tool in the documented `POST /v1/convai/tools` shape."""
    return {
        "tool_config": {
            "type": "webhook",
            "name": tool.name,
            "description": tool.description,
            "api_schema": {
                "url": tool.url,
                "method": tool.method,
                "request_headers": dict(tool.headers),
                "request_body_schema": {
                    "type": "object",
                    "properties": {
                        p.name: {"type": p.type, "description": p.description}
                        for p in tool.body_params
                    },
                    "required": [p.name for p in tool.body_params if p.required],
                },
            },
        }
    }


def tool_payloads(
    *, base_url: str, clinic_id: UUID, token_ref: str = "{{secret__agent_token}}"
) -> list[dict[str, Any]]:
    """All seven tools.

    `token_ref` defaults to a workspace-secret placeholder rather than the literal token: store
    the value once in ElevenLabs and reference it, so the shared secret is not copied into seven
    tool configs (or into a file that reaches git).
    """
    return [tool_payload(t) for t in _tools(base_url, clinic_id, token_ref)]


def agent_config(
    *, clinic_name: str, prompt: str, first_message: str, language: str = "sv"
) -> dict[str, Any]:
    """The contents of `agent_configs/<slug>.json`, and the body of an agent PATCH.

    Note what is absent: no `llm`, no `tts`, no `turn`. Those are chosen by ear in the dashboard
    and a push must not overwrite them.
    """
    return {
        "name": clinic_name,
        "conversation_config": {
            "agent": {
                "prompt": {"prompt": prompt},
                "first_message": first_message,
                "language": language,
            }
        },
    }


def agents_registry(*, slug: str, agent_id: str | None) -> dict[str, Any]:
    """`agents.json` — the registry the official `elevenlabs agents push` reads."""
    entry: dict[str, Any] = {"config": f"agent_configs/{slug}.json"}
    if agent_id:
        entry["id"] = agent_id
    return {"agents": [entry]}


# --- pushing ---------------------------------------------------------------------------------


@dataclass
class PushResult:
    agent_id: str
    tools_created: list[str] = field(default_factory=list)
    tools_updated: list[str] = field(default_factory=list)


class PushError(RuntimeError):
    pass


def _client(settings: Settings) -> httpx.AsyncClient:
    if settings.elevenlabs_api_key is None:
        raise PushError("VAKANS_ELEVENLABS_API_KEY is not set.")
    return httpx.AsyncClient(
        base_url=API_ROOT,
        timeout=API_TIMEOUT_S,
        headers={"xi-api-key": settings.elevenlabs_api_key.get_secret_value()},
    )


async def _existing_tool_ids(http: httpx.AsyncClient) -> dict[str, str]:
    response = await http.get("/tools")
    response.raise_for_status()
    body = response.json() or {}
    return {
        t["tool_config"]["name"]: t["id"]
        for t in body.get("tools", [])
        if t.get("tool_config", {}).get("name")
    }


async def push(
    *,
    settings: Settings,
    clinic_name: str,
    clinic_id: UUID,
    base_url: str,
    prompt: str,
    first_message: str,
    token_ref: str = "{{secret__agent_token}}",
) -> PushResult:
    """Upsert the seven tools, then point the agent at them.

    Idempotent: tools are matched by name, so running this twice updates rather than duplicates.
    That matters because the tunnel URL changes on every `cloudflared` restart, which means this
    gets run a lot.
    """
    agent_id = settings.elevenlabs_agent_id
    if not agent_id:
        raise PushError(
            "VAKANS_ELEVENLABS_AGENT_ID is not set. Create the agent once in the dashboard "
            "(that is where you pick its voice), then put its id in .env."
        )

    result = PushResult(agent_id=agent_id)
    async with _client(settings) as http:
        existing = await _existing_tool_ids(http)

        tool_ids: list[str] = []
        for payload in tool_payloads(base_url=base_url, clinic_id=clinic_id, token_ref=token_ref):
            name = payload["tool_config"]["name"]
            if name in existing:
                response = await http.patch(f"/tools/{existing[name]}", json=payload)
                _raise_for(response, f"update tool {name}")
                tool_ids.append(existing[name])
                result.tools_updated.append(name)
            else:
                response = await http.post("/tools", json=payload)
                _raise_for(response, f"create tool {name}")
                tool_ids.append(response.json()["id"])
                result.tools_created.append(name)

        body = agent_config(clinic_name=clinic_name, prompt=prompt, first_message=first_message)
        body["conversation_config"]["agent"]["prompt"]["tool_ids"] = tool_ids
        response = await http.patch(f"/agents/{agent_id}", json=body)
        _raise_for(response, "update agent")

    return result


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.is_error:
        # The body carries the actual validation complaint; the status alone is useless when a
        # field name has drifted.
        raise PushError(f"Could not {what}: {response.status_code} {response.text[:400]}")


def push_sync(**kwargs: Any) -> PushResult:
    """`asyncio.run` wrapper, for the Typer CLI."""
    return asyncio.run(push(**kwargs))


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
