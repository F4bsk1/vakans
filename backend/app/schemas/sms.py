"""Response model for the inbound SMS webhook.

46elks ignores the body and only looks at the status code, so this exists for us: `curl`-ing the
endpoint during a rehearsal should say plainly whether the JA landed.
"""

from __future__ import annotations

from pydantic import BaseModel


class InboundSmsResponse(BaseModel):
    accepted: bool
    answer: str | None = None
    reason: str | None = None
    booking_ref: str | None = None
    duplicate: bool = False
