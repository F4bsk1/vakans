"""Outbound SMS — an outbox, not a client call.

`send_sms` never touches the network. It writes a `queued` row and returns; `deliver_queued`
drains those rows to the provider, and `app.refill.worker` calls it on a timer. Three reasons
that split is worth a little indirection:

1. Cancelling a booking mid-call must not cost an HTTP round trip to a Swedish SMS carrier.
   The refill cascade fires from inside a voice turn with a 1.5-second budget.
2. Delivery retries become a `select ... where status = 'queued'` instead of retry logic
   scattered through every caller.
3. Rolling back the transaction rolls back the SMS. A message can never be delivered for a
   booking that did not commit — the same reasoning as the events outbox in `app.events`.

Every send goes through `phone.may_send_sms`, so the fabricated `+4670…` numbers in the demo
database can never receive a real text — see app/phone.py for why that matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import SmsDirection, SmsMessage, SmsStatus
from app.phone import may_send_sms, normalize_e164

logger = logging.getLogger("vakans.sms")

ELKS_SMS_URL = "https://api.46elks.com/a1/sms"
ELKS_TIMEOUT_S = 10.0


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
        # Expected for every seeded patient whenever a real provider is configured. Left as
        # `failed`, which means `deliver_queued` will never pick it up — the guard is enforced
        # once, at write time, and cannot be forgotten at delivery time.
        logger.info("suppressed SMS to %s (not the founder mobile)", normalized)

    return row


# --- delivery ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryResult:
    sent: int = 0
    failed: int = 0


async def _deliver_via_elks(row: SmsMessage, settings: Settings) -> tuple[bool, str | None]:
    """One 46elks send. Returns (ok, provider_message_id)."""
    if settings.elks_username is None or settings.elks_password is None:
        logger.error("sms_provider=46elks but VAKANS_ELKS_USERNAME/PASSWORD are not set")
        return False, None

    try:
        async with httpx.AsyncClient(timeout=ELKS_TIMEOUT_S) as http:
            response = await http.post(
                ELKS_SMS_URL,
                auth=(settings.elks_username, settings.elks_password.get_secret_value()),
                data={
                    "from": settings.sms_sender_id,
                    "to": row.to_phone,
                    "message": row.body,
                },
            )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Do not re-raise: one unreachable carrier must not stop the worker tick, and the row
        # is left `queued` so the next tick retries it.
        logger.warning("46elks send failed for %s: %s", row.id, exc)
        return False, None

    return True, (response.json() or {}).get("id")


async def deliver_queued(
    session: AsyncSession, *, settings: Settings, limit: int = 20
) -> DeliveryResult:
    """Drain the outbox. Called by the refill worker; safe to call from anywhere.

    `skip_locked` so two workers never send the same message twice — the row is claimed by
    whoever locks it first and the other one moves on.
    """
    rows = list(
        (
            await session.execute(
                select(SmsMessage)
                .where(
                    SmsMessage.direction == SmsDirection.OUT,
                    SmsMessage.status == SmsStatus.QUEUED,
                )
                .order_by(SmsMessage.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    if not rows:
        return DeliveryResult()

    sent = failed = 0
    for row in rows:
        if settings.sms_provider == "fake":
            # Nothing leaves the building. Logged at info so a rehearsal can read the cascade
            # off the API log without a phone in hand.
            logger.info("[fake-sms] -> %s: %s", row.to_phone, row.body)
            row.status = SmsStatus.SENT
            sent += 1
            continue

        ok, message_id = await _deliver_via_elks(row, settings)
        if ok:
            row.status = SmsStatus.SENT
            row.provider_message_id = message_id
            sent += 1
        else:
            failed += 1

    await session.flush()
    return DeliveryResult(sent=sent, failed=failed)
