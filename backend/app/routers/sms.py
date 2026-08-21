"""Inbound SMS — the "JA" that refills a slot. Spec §3 step 4.

46elks POSTs a form body to whatever URL is configured on the number. It cannot send custom
headers, so unlike `/agent/*` this endpoint is authenticated with a query-string token:

    https://<tunnel>/sms/<clinic_id>/inbound?token=<VAKANS_AGENT_TOKEN>

That is a real tradeoff — a query string ends up in access logs in a way a header does not —
taken because the alternative is an unauthenticated public endpoint that can book appointments,
and because the demo's inbound path carries no patient data beyond a phone number and the word
"JA". Reusing `VAKANS_AGENT_TOKEN` rather than minting a second secret is deliberate: one value
to rotate, one value to leak.

The handler is thin on purpose. Everything below `engine.record_reply` is provider-neutral, so
swapping 46elks for Twilio means editing this file and nothing else.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.refill import engine
from app.schemas.sms import InboundSmsResponse
from app.timeutils import now_utc

logger = logging.getLogger("vakans.sms.inbound")

router = APIRouter(prefix="/sms", tags=["sms"])


def _require_token(token: str | None, settings: Settings) -> None:
    """Fail-closed, for the same reason `/agent/*` is: a tunnel makes localhost public."""
    if settings.env == "test":
        return
    if settings.agent_token is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="VAKANS_AGENT_TOKEN is not set; the inbound SMS webhook cannot be secured.",
        )
    if token != settings.agent_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


@router.post("/{clinic_id}/inbound", response_model=InboundSmsResponse)
async def inbound_sms(
    clinic_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str | None, Query()] = None,
    # 46elks' field names. `id` is the idempotency key: it retries any delivery it did not get
    # a 2xx for, and booking the same slot twice off a redelivery would be a memorable way to
    # lose a clinic's trust.
    id: Annotated[str | None, Form()] = None,
    from_: Annotated[str | None, Form(alias="from")] = None,
    to: Annotated[str | None, Form()] = None,
    message: Annotated[str | None, Form()] = None,
    direction: Annotated[str | None, Form()] = None,
) -> InboundSmsResponse:
    _require_token(token, settings)

    if not from_:
        # Answer 200 anyway: a 4xx makes 46elks retry a message we will never understand.
        logger.warning("inbound SMS with no sender for clinic %s", clinic_id)
        return InboundSmsResponse(accepted=False, reason="no_sender")

    result = await engine.record_reply(
        session,
        clinic_id=clinic_id,
        from_phone=from_,
        body=message,
        settings=settings,
        now=now_utc(),
        provider="46elks",
        provider_message_id=id,
    )
    await session.commit()

    return InboundSmsResponse(
        accepted=result.accepted,
        answer=result.answer.value,
        reason=result.reason,
        booking_ref=result.booking_ref,
        duplicate=result.duplicate,
    )
