"""The dashboard's snapshot and its live socket."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import EventEnvelope, EventType, emit, get_event_bus
from tests.integration.test_seed_and_reset import NOW, make_clinic

AT = {"now": NOW.isoformat()}
RESET_BODY = {"now": NOW.isoformat()}


async def _armed(client: AsyncClient, db_session: AsyncSession, slug: str = "demo"):
    clinic = await make_clinic(db_session, slug)
    await db_session.commit()
    assert (await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)).status_code == 200
    return clinic


async def _state(client: AsyncClient, clinic) -> dict:
    response = await client.get(f"/dash/{clinic.id}/state", params=AT)
    assert response.status_code == 200, response.text
    return response.json()


# --- the snapshot ---------------------------------------------------------------------------


async def test_state_returns_a_week_of_calendar(client: AsyncClient, db_session) -> None:
    clinic = await _armed(client, db_session)

    body = await _state(client, clinic)

    assert body["clinic"]["slug"] == "demo"
    assert len(body["days"]) == 5  # five weekdays, what fits a laptop without scrolling
    # Today forward, never the calendar week: on a Friday the ISO week is entirely history,
    # including the slot a running cascade is texting people about.
    assert body["days"][0]["is_today"]
    assert all(day["label"][:3] not in ("lör", "sön") for day in body["days"])
    assert body["resources"]
    assert any(day["slots"] for day in body["days"])
    assert body["range_start"] <= body["range_end"]
    assert "aug" in body["range_label"] or "sep" in body["range_label"]


async def test_every_human_string_is_rendered_server_side(client: AsyncClient, db_session) -> None:
    """The browser must never format a Swedish date or a clock time — same rule as `speech`."""
    body = await _state(client, await _armed(client, db_session))

    day = body["days"][0]
    assert day["label"][:3] in ("mån", "tis", "ons", "tor", "fre", "lör", "sön")
    slot = next(s for d in body["days"] for s in d["slots"])
    assert len(slot["label"]) == 5 and slot["label"][2] == ":"  # "08:00"


async def test_booked_slots_carry_who_what_and_the_risk_band(
    client: AsyncClient, db_session
) -> None:
    body = await _state(client, await _armed(client, db_session))

    booked = [s for d in body["days"] for s in d["slots"] if s["status"] == "booked"]
    assert booked
    assert all(s["patient_name"] and s["service"] for s in booked)
    assert all(
        s["risk_band"] in ("green", "yellow", "red") for s in booked if s["risk"] is not None
    )


async def test_kpis_add_up(client: AsyncClient, db_session) -> None:
    body = await _state(client, await _armed(client, db_session))
    k = body["kpis"]

    assert k["slots_total"] == sum(len(d["slots"]) for d in body["days"])
    assert k["slots_open"] + k["slots_booked"] + k["slots_held"] <= k["slots_total"]
    assert 0.0 <= k["fill_rate"] <= 1.0
    assert k["risk_green"] + k["risk_yellow"] + k["risk_red"] > 0


async def test_a_refill_moves_the_money_from_at_risk_to_recovered(
    client: AsyncClient, db_session
) -> None:
    """The ROI counter is summed from `events`, so it is the same number a PDF would show."""
    clinic = await _armed(client, db_session)
    before = (await _state(client, clinic))["kpis"]

    cancelled = (await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)).json()
    freed = (await _state(client, clinic))["kpis"]
    assert freed["at_risk_sek"] > before["at_risk_sek"]
    assert freed["slots_held"] == before["slots_held"] + 1

    await client.post(f"/sim/{clinic.id}/waitlist_reply", json={"answer": "JA", **AT})
    after = (await _state(client, clinic))["kpis"]

    assert after["recovered_sek"] > 0
    assert after["at_risk_sek"] < freed["at_risk_sek"]
    assert after["bookings_by_refill"] == before["bookings_by_refill"] + 1
    assert cancelled["cascade"]["outcome"] == "offered"


async def test_a_cascade_is_returned_as_an_ordered_timeline(
    client: AsyncClient, db_session
) -> None:
    clinic = await _armed(client, db_session)
    await client.post(f"/sim/{clinic.id}/cancel_random", json=AT)
    await client.post(f"/sim/{clinic.id}/waitlist_reply", json={"answer": "NEJ", **AT})

    body = await _state(client, clinic)

    assert len(body["cascades"]) == 1
    cascade = body["cascades"][0]
    assert [o["candidate_n"] for o in cascade["offers"]] == [1, 2]
    assert cascade["offers"][0]["status"] == "declined"
    assert "kl" in cascade["slot_label"]  # pre-rendered Swedish, not a raw timestamp
    assert cascade["outcome"] in ("live", "refilled", "exhausted")


async def test_state_hands_back_the_id_the_socket_should_resume_from(
    client: AsyncClient, db_session
) -> None:
    """Without this the event emitted during the WS handshake is lost, and the cascade looks
    like it silently skipped a candidate."""
    body = await _state(client, await _armed(client, db_session))

    assert body["last_event_id"] > 0
    assert body["events"]
    assert body["last_event_id"] == max(e["id"] for e in body["events"])


async def test_unknown_clinic_is_404(client: AsyncClient, db_session) -> None:
    assert (await client.get(f"/dash/{uuid.uuid4()}/state")).status_code == 404


# --- the socket -----------------------------------------------------------------------------
#
# The bus itself, and the after-commit publication that feeds it, are covered in
# test_events.py. What is left to prove here is the wiring: that a socket replays what it
# missed, and that the pump forwards and pings.
#
# Deliberately *not* tested through TestClient: it runs the app in its own thread with its own
# event loop, so publishing to the in-process bus from the test thread is a cross-thread poke
# at an asyncio.Queue. It happens to work often enough to produce a flaky test and it is not
# how anything behaves in production, where the bus and the socket share one loop.


@pytest.mark.db_commit
async def test_the_socket_replays_what_was_missed_during_the_handshake(
    committing_db_session: AsyncSession, engine
) -> None:
    """Without this, an event landing during the handshake vanishes — which on stage looks
    like the cascade silently skipping a candidate."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db import get_session
    from app.main import create_app

    clinic = await make_clinic(committing_db_session, "ws-replay")
    seen = await emit(
        committing_db_session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CREATED,
        payload={"booking_ref": "OLD001"},
        actor="test",
    )
    missed = await emit(
        committing_db_session,
        clinic_id=clinic.id,
        type=EventType.BOOKING_CREATED,
        payload={"booking_ref": "NEW002"},
        actor="test",
    )
    await committing_db_session.commit()

    app = create_app()
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override

    with TestClient(app).websocket_connect(
        f"/dash/{clinic.id}/events?after_id={seen.id}"
    ) as socket:
        received = json.loads(socket.receive_text())

    assert received["id"] == missed.id
    assert received["payload"]["booking_ref"] == "NEW002"


async def test_the_pump_forwards_every_event_it_is_given() -> None:
    from app.routers.dash import _pump

    sent: list[str] = []

    class FakeSocket:
        async def send_text(self, text: str) -> None:
            sent.append(text)
            if len(sent) == 2:
                raise _Stop

    async def stream():
        for ref in ("A1", "B2"):
            yield EventEnvelope(
                id=len(sent) + 1,
                clinic_id=uuid.uuid4(),
                ts=NOW,
                type="booking_created",
                payload={"booking_ref": ref},
            )

    with pytest.raises(_Stop):
        await _pump(FakeSocket(), stream())

    assert [json.loads(m)["payload"]["booking_ref"] for m in sent] == ["A1", "B2"]


async def test_the_pump_pings_an_idle_socket(monkeypatch) -> None:
    """A socket that never writes cannot tell a dropped connection from a quiet clinic, and an
    idle proxy will close it."""
    from app.routers import dash

    monkeypatch.setattr(dash, "PING_INTERVAL_S", 0.01)
    sent: list[str] = []

    class FakeSocket:
        async def send_text(self, text: str) -> None:
            sent.append(text)
            raise _Stop

    async def silent():
        await asyncio.Event().wait()
        yield  # pragma: no cover

    with pytest.raises(_Stop):
        await dash._pump(FakeSocket(), silent())

    assert json.loads(sent[0]) == {"type": "ping"}


class _Stop(Exception):
    """Ends the pump loop from inside a fake socket, the way a disconnect would."""


def test_the_bus_is_the_seam_the_socket_subscribes_to() -> None:
    """Decision D3: swapping InProcessEventBus for a PgNotify one must not touch the router."""
    bus = get_event_bus()
    assert hasattr(bus, "subscribe")
    assert hasattr(bus, "publish_nowait")
