"""The demo, driven through HTTP exactly the way an operator drives it.

`test_the_whole_demo_beat` is the acceptance test for step 3: reset, cancel, cascade, JA,
refilled, with a SEK figure. If that one passes, the story we sell works end to end.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, BookingStatus, Patient, RefillOffer, Slot, SlotStatus
from tests.integration.test_seed_and_reset import NOW, make_clinic

RESET_BODY = {"now": NOW.isoformat()}
# Every sim endpoint takes a pinned `now`, and the sandbox is seeded for one — a demo
# replayed against a fixed clock has to agree with itself about what "upcoming" means.
AT = {"now": NOW.isoformat()}


async def _armed(client: AsyncClient, db_session: AsyncSession, slug: str = "demo"):
    clinic = await make_clinic(db_session, slug)
    await db_session.commit()
    response = await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    assert response.status_code == 200, response.text
    return clinic


async def test_the_whole_demo_beat(client: AsyncClient, db_session: AsyncSession) -> None:
    clinic = await _armed(client, db_session)

    cancelled = (await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)).json()
    assert cancelled["cascade"]["outcome"] == "offered"
    assert cancelled["cascade"]["candidate_n"] == 1
    assert cancelled["sek_at_risk"] is None or cancelled["sek_at_risk"] > 0
    # The operator should be able to read this line out loud without opening the dashboard.
    assert "kl" in cancelled["speech"]

    reply = (
        await client.post(f"/sim/{clinic.id}/waitlist_reply", json={"answer": "JA", **AT})
    ).json()
    assert reply["accepted"] is True
    assert reply["booking_ref"]
    assert reply["cascade_id"] == cancelled["cascade"]["cascade_id"]

    slot_id = uuid.UUID(cancelled["cascade"]["slot_id"])
    slot = await db_session.get(Slot, slot_id)
    await db_session.refresh(slot)
    assert slot.status is SlotStatus.BOOKED

    refilled = (
        (
            await db_session.execute(
                select(Booking).where(
                    Booking.slot_id == slot_id, Booking.status == BookingStatus.CONFIRMED
                )
            )
        )
        .scalars()
        .one()
    )
    assert refilled.booked_via == "refill"
    assert refilled.booking_ref == reply["booking_ref"]


async def test_cancel_random_prefers_a_slot_the_cascade_can_actually_fill(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A cascade with nobody to text is a correct outcome and a dead demo."""
    clinic = await _armed(client, db_session)

    body = (await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)).json()

    assert body["cascade"]["outcome"] == "offered"
    assert body["cascade"]["patient_name"]


async def test_cancel_random_can_target_a_specific_booking(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """For a rehearsed demo you want the same booking every time, not a surprise."""
    clinic = await _armed(client, db_session)
    booking = (
        await db_session.execute(
            select(Booking, Slot)
            .join(Slot, Booking.slot_id == Slot.id)
            .where(
                Booking.clinic_id == clinic.id,
                Booking.status == BookingStatus.CONFIRMED,
                Slot.starts_at > NOW,
            )
            .order_by(Slot.starts_at)
        )
    ).first()[0]

    body = (
        await client.post(
            f"/sim/{clinic.id}/cancel_random", json={"booking_ref": booking.booking_ref, **AT}
        )
    ).json()

    assert body["booking_ref"] == booking.booking_ref


async def test_waitlist_reply_without_an_offer_is_a_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Better a clear 404 than a silent no-op the operator discovers on stage."""
    clinic = await _armed(client, db_session)

    response = await client.post(f"/sim/{clinic.id}/waitlist_reply", json={"answer": "JA", **AT})

    assert response.status_code == 404


async def test_a_no_over_the_sim_endpoint_advances_the_cascade(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    clinic = await _armed(client, db_session)
    cancelled = (await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)).json()

    declined = (
        await client.post(f"/sim/{clinic.id}/waitlist_reply", json={"answer": "NEJ", **AT})
    ).json()
    assert declined["accepted"] is False

    offers = (
        (
            await db_session.execute(
                select(RefillOffer)
                .where(RefillOffer.cascade_id == uuid.UUID(cancelled["cascade"]["cascade_id"]))
                .order_by(RefillOffer.candidate_n)
            )
        )
        .scalars()
        .all()
    )
    assert [o.candidate_n for o in offers] == [1, 2]


async def test_cancel_random_needs_a_seeded_sandbox(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    clinic = await make_clinic(db_session, "tom")
    await db_session.commit()

    response = await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)

    assert response.status_code == 404
    assert "reset" in response.json()["detail"]


# --- the inbound webhook -------------------------------------------------------------------------


async def test_the_46elks_webhook_books_the_slot(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The path the JA-svararen's real phone takes."""
    clinic = await _armed(client, db_session)
    cancelled = (await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)).json()

    offer = (
        (
            await db_session.execute(
                select(RefillOffer).where(
                    RefillOffer.cascade_id == uuid.UUID(cancelled["cascade"]["cascade_id"])
                )
            )
        )
        .scalars()
        .one()
    )
    patient = await db_session.get(Patient, offer.patient_id)

    response = await client.post(
        f"/sms/{clinic.id}/inbound",
        data={
            "id": "sabc123",
            "from": patient.phone,
            "to": "+46766861010",
            "message": "Ja tack!",
            "direction": "incoming",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["answer"] == "ja"
    assert body["booking_ref"]


async def test_the_webhook_answers_200_to_a_message_it_cannot_use(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A 4xx makes 46elks retry forever something we will never understand."""
    clinic = await _armed(client, db_session)

    response = await client.post(
        f"/sms/{clinic.id}/inbound",
        data={"id": "s1", "to": "+46766861010", "message": "JA", "direction": "incoming"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is False
