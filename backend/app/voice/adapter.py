"""Voice orchestration adapter interface.

Charter decision D1 defers the Retell-vs-Pipecat choice to step 4 but requires this interface
to exist from day one. The point is that *all* tool logic lives in our FastAPI backend, so the
voice layer stays a thin adapter and swapping Retell for Pipecat leaves the sandbox, agent
tools and dashboard untouched.

Nothing implements this yet. Step 4 adds `retell_adapter.py` (and later `pipecat_app.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ToolCall:
    """A function call the voice platform wants us to execute."""

    name: str
    arguments: dict[str, Any]
    call_id: str


@dataclass(frozen=True)
class ToolResult:
    """The canonical agent-tool return shape from the charter's coding conventions.

    `speech` is what the agent says verbatim — the backend formats it so the LLM never has to
    render Swedish dates or prices. `data` is what the dashboard receives via an event.
    """

    speech: str
    data: dict[str, Any]


@dataclass(frozen=True)
class CallContext:
    clinic_id: UUID
    call_sid: str
    from_phone: str | None


class VoiceAdapter(Protocol):
    """What any voice platform must provide for us to run a Swedish clinic call."""

    async def on_call_started(self, ctx: CallContext) -> str:
        """Return the mandatory opening line.

        EU AI Act art. 50: this must disclose that the caller is talking to an AI. That is
        non-negotiable and hardcoded — never make it prompt-tunable or clinic-configurable.
        """
        ...

    async def on_tool_call(self, ctx: CallContext, call: ToolCall) -> ToolResult: ...

    async def on_transcript(self, ctx: CallContext, text: str, *, is_final: bool) -> None: ...

    async def on_call_ended(self, ctx: CallContext) -> None: ...
