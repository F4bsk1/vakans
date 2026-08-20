"""Outbound SMS.

Step 3b swaps in the real 46elks sender; this records the message and enforces the guard.
Every send goes through `phone.may_send_sms`, so the fabricated `+4670…` numbers in the demo
database can never receive a real text — see app/phone.py for why that matters.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import SmsDirection, SmsMessage, SmsStatus
from app.phone import may_send_sms, normalize_e164

logger = logging.getLogger("vakans.sms")


async def send_sms(
    session: AsyncSession,
    *,
    clinic_id: UUID,
    to_phone: str | None,
    body: str,
    settings: Settings,
    patient_id: UUID | None = None,
    refill_offer_id: UUID | None = None,
) -> SmsMessage | None:
    """Record an outbound SMS, and actually send it only if the guard allows.

    Returns the row so callers can reference it in an event payload. Returns None when there is
    no number to send to.
    """
    normalized = normalize_e164(to_phone)
    if normalized is None:
        logger.warning("no usable phone number for clinic %s, not sending", clinic_id)
        return None

    allowed = may_send_sms(
        normalized,
        sms_provider=settings.sms_provider,
        founder_mobile=settings.founder_mobile,
    )

    row = SmsMessage(
        id=uuid4(),
        clinic_id=clinic_id,
        direction=SmsDirection.OUT,
        to_phone=normalized,
        from_phone=settings.sms_sender_id,
        body=body,
        provider=settings.sms_provider,
        # `queued` rather than `sent`: nothing has left the building yet. Step 3b flips this
        # to `sent` when 46elks accepts it, and to `delivered` on the delivery webhook.
        status=SmsStatus.QUEUED if allowed else SmsStatus.FAILED,
        patient_id=patient_id,
        refill_offer_id=refill_offer_id,
    )
    session.add(row)
    await session.flush()

    if not allowed:
        # Expected for every seeded patient whenever a real provider is configured.
        logger.info("suppressed SMS to %s (not the founder mobile)", normalized)

    return row
