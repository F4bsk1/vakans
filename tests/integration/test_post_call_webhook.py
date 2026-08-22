"""The post-call webhook — the only place we ever see what the caller actually said."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import recent_events
from app.routers.calls import TranscriptTurn, audit_boundary
from tests.integration.test_seed_and_reset import make_clinic


def turn(role: str, message: str) -> dict:
    return {"role": role, "message": message}


def body(*turns: dict, conversation_id: str = "conv_1") -> dict:
    return {
        "type": "post_call_transcription",
        "event_timestamp": 1_780_000_000,
        "data": {
            "agent_id": "agent_1",
            "conversation_id": conversation_id,
            "status": "done",
            "transcript": list(turns),
        },
    }


# --- the audit (pure) ---------------------------------------------------------------------


def test_the_audit_reads_the_callers_turns_not_the_agents() -> None:
    """The agent saying "bröstsmärta" back is not a red flag; the caller saying it is."""
    turns = [
        TranscriptTurn(role="agent", message="Nämn inte bröstsmärta i det här sammanhanget"),
        TranscriptTurn(role="user", message="jag vill boka en massage"),
    ]

    said, flags = audit_boundary(turns)

    assert said == ["jag vill boka en massage"]
    assert flags == []


def test_the_audit_catches_what_the_live_check_cannot() -> None:
    """During a call we only see tool arguments — "massage" would have reached us, not this."""
    turns = [
        TranscriptTurn(role="user", message="jag har ont i bröstet men vill boka massage"),
        TranscriptTurn(role="agent", message="Visst, jag har en tid imorgon."),
    ]

    _, flags = audit_boundary(turns)

    assert "bröstsmärta" in flags


# --- the endpoint -------------------------------------------------------------------------


async def test_a_transcript_is_recorded_as_an_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    clinic = await make_clinic(db_session, "postcall")
    await db_session.commit()

    response = await client.post(
        f"/calls/{clinic.id}/post-call",
        json=body(
            turn("agent", "Hej, du pratar med vår digitala assistent."),
            turn("user", "jag vill boka en tid för massage"),
            turn("agent", "Jag har imorgon kl 10 hos Johanna."),
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "turns": 3,
        "caller_turns": 1,
        "missed_red_flags": [],
        "reason": None,
    }

    events = await recent_events(db_session, clinic.id)
    transcript = next(e for e in events if e.type == "call_transcript")
    assert len(transcript.payload["turns"]) == 3
    assert transcript.payload["conversation_id"] == "conv_1"


async def test_a_missed_red_flag_is_recorded_as_an_escalation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The reason this endpoint exists: the caller raised something medical and the agent
    carried on booking. That must not be swallowed."""
    clinic = await make_clinic(db_session, "missed")
    await db_session.commit()

    response = await client.post(
        f"/calls/{clinic.id}/post-call",
        json=body(
            turn("user", "jag har ont i bröstet, kan jag boka en tid?"),
            turn("agent", "Absolut, jag har en tid imorgon kl 10."),
        ),
    )

    assert response.json()["missed_red_flags"] == ["bröstsmärta"]

    events = await recent_events(db_session, clinic.id)
    escalation = next(e for e in events if e.type == "escalation")
    assert escalation.payload["reason"] == "missed_medical_red_flag"
    assert escalation.actor == "audit"


async def test_a_handled_red_flag_is_not_reported_as_missed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The agent did refer them on, so this is the boundary working, not failing."""
    clinic = await make_clinic(db_session, "handled")
    await db_session.commit()

    response = await client.post(
        f"/calls/{clinic.id}/post-call",
        json=body(
            turn("user", "jag har ont i bröstet"),
            turn("agent", "Ring 112 om det är akut, annars 1177."),
        ),
    )

    assert response.json()["missed_red_flags"] == []
    events = await recent_events(db_session, clinic.id)
    assert not [e for e in events if e.type == "escalation"]


async def test_other_webhook_types_are_acknowledged_and_ignored(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-200 would make them retry an audio payload we will never use."""
    clinic = await make_clinic(db_session, "audio")
    await db_session.commit()

    response = await client.post(
        f"/calls/{clinic.id}/post-call",
        json={"type": "post_call_audio", "data": {"conversation_id": "c2"}},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is False


async def test_unknown_clinic_is_404(client: AsyncClient) -> None:
    response = await client.post(f"/calls/{uuid.uuid4()}/post-call", json=body())
    assert response.status_code == 404
