"""ElevenLabs post-call webhook — the only time we ever see what was actually said.

During a call we receive nothing but tool arguments (see `app.agent.safety`). Afterwards,
ElevenLabs POSTs the whole conversation here. That makes two things possible that were not
before:

1. **A real audit of the medical boundary.** We can run `safety.check` over the caller's own
   turns rather than over the fields the model chose to forward. When that finds a red flag the
   agent never escalated, it is recorded as a `missed` escalation — the honest version of "we
   have a medical boundary", and far more convincing to a clinic than a regex we promise runs.

2. **A transcript on the dashboard.** Spec §6 lists `call_transcript`; this is what fills it.

**Authentication.** ElevenLabs signs deliveries with an `ElevenLabs-Signature` header, but their
docs do not publish the string format — they direct you to their SDK's `construct_event`. Rather
than guess at security-critical code, this endpoint is gated the same way the 46elks webhook is:
a shared token in the query string, which works because we paste the URL into their dashboard
ourselves. The signature is stored on the event so HMAC verification can be added later against
a real delivery instead of against a guess.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import safety
from app.config import Settings, get_settings
from app.db import get_session
from app.events import EventType, emit
from app.models import Clinic

logger = logging.getLogger("vakans.calls")

router = APIRouter(prefix="/calls", tags=["calls"])

# Charter D6: transcripts live seven days. Storing them on the event row means the purge is a
# delete by age over `events` rather than a second retention policy on a second table.
MAX_TURNS_STORED = 200


class TranscriptTurn(BaseModel):
    """One turn. `extra="allow"` on purpose — their payload carries timing and tool metadata we
    do not use today, and dropping it on the floor would make a future need a schema change."""

    model_config = ConfigDict(extra="allow")

    role: str | None = None
    message: str | None = None


class PostCallData(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str | None = None
    conversation_id: str | None = None
    status: str | None = None
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    analysis: dict[str, Any] = Field(default_factory=dict)


class PostCallWebhook(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str | None = None
    event_timestamp: int | None = None
    data: PostCallData = Field(default_factory=PostCallData)


class PostCallResponse(BaseModel):
    accepted: bool
    turns: int = 0
    caller_turns: int = 0
    missed_red_flags: list[str] = Field(default_factory=list)
    reason: str | None = None


def _authorize(token: str | None, settings: Settings) -> None:
    if settings.env == "test":
        return
    if settings.agent_token is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="VAKANS_AGENT_TOKEN is not set; the post-call webhook cannot be secured.",
        )
    if token != settings.agent_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


def audit_boundary(turns: list[TranscriptTurn]) -> tuple[list[str], list[str]]:
    """Re-run the medical boundary over what the caller actually said.

    Returns (caller messages, red-flag labels). This is the check we *cannot* do live, because
    during a call we only ever see the arguments the model chose to send us.
    """
    said = [t.message for t in turns if t.role == "user" and t.message]
    verdict = safety.check(*said)
    return said, list(verdict.matched)


@router.post("/{clinic_id}/post-call", response_model=PostCallResponse)
async def post_call(
    clinic_id: UUID,
    payload: PostCallWebhook,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Query()] = None,
    elevenlabs_signature: Annotated[str | None, Header()] = None,
) -> PostCallResponse:
    _authorize(token, settings)

    clinic = (
        await session.execute(select(Clinic).where(Clinic.id == clinic_id))
    ).scalar_one_or_none()
    if clinic is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="clinic not found")

    # Anything that is not a transcription webhook (audio, call-failure) is acknowledged and
    # ignored: answering non-200 would make them retry something we will never use.
    if payload.type and payload.type != "post_call_transcription":
        return PostCallResponse(accepted=False, reason=f"ignored type {payload.type}")

    turns = payload.data.transcript[:MAX_TURNS_STORED]
    said, red_flags = audit_boundary(turns)
    escalated = any(t.role == "agent" and t.message and "1177" in t.message for t in turns)

    await emit(
        session,
        clinic_id=clinic.id,
        type=EventType.CALL_TRANSCRIPT,
        payload={
            "conversation_id": payload.data.conversation_id,
            "status": payload.data.status,
            "turns": [
                {"role": t.role, "message": t.message} for t in turns if t.role and t.message
            ],
            "caller_turns": len(said),
            # Kept so HMAC verification can be added later against a real delivery rather than
            # against a guess at the format.
            "signature": elevenlabs_signature,
            "source_ip": request.client.host if request.client else None,
        },
        actor="elevenlabs",
    )

    if red_flags and not escalated:
        # The interesting case, and the reason this endpoint exists: the caller raised something
        # medical and the agent never referred them on. Recorded rather than swallowed.
        logger.warning(
            "medical red flag not escalated in conversation %s: %s",
            payload.data.conversation_id,
            red_flags,
        )
        await emit(
            session,
            clinic_id=clinic.id,
            type=EventType.ESCALATION,
            payload={
                "reason": "missed_medical_red_flag",
                "matched": red_flags,
                "conversation_id": payload.data.conversation_id,
                "source": "post_call_audit",
            },
            actor="audit",
        )

    await session.commit()
    return PostCallResponse(
        accepted=True,
        turns=len(turns),
        caller_turns=len(said),
        missed_red_flags=[] if escalated else red_flags,
    )
