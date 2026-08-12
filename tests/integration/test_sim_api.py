"""The /sim endpoints: the reset contract and the QA gate it enforces."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Slot
from tests.integration.test_seed_and_reset import NOW, make_clinic

RESET_BODY = {"now": NOW.isoformat()}


async def test_reset_arms_a_demo_and_reports_what_is_ready(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    clinic = await make_clinic(db_session)

    resp = await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["slug"] == "demo"
    assert body["counts"]["slots"] > 300
    assert 3 <= len(body["demo_slots"]) <= 5
    assert body["demo_slot_strategy"] == "today_pm_tomorrow_am"
    assert body["fingerprint"]
    # The operator should be able to read the armed slots aloud without opening the dashboard.
    for slot in body["demo_slots"]:
        assert slot["resource_name"]
        assert "kl" in slot["speech"]


async def test_reset_speech_is_pre_formatted_swedish(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The backend owns Swedish time rendering — never the LLM. Same trick as the agent tools."""
    clinic = await make_clinic(db_session)
    body = (await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)).json()
    speeches = [s["speech"] for s in body["demo_slots"]]
    assert any(s.startswith(("idag", "imorgon")) for s in speeches), speeches


async def test_reset_is_idempotent_for_a_fixed_now(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    clinic = await make_clinic(db_session)
    first = (await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)).json()
    second = (await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)).json()

    assert first["fingerprint"] == second["fingerprint"]
    assert first["counts"] == second["counts"]


async def test_reset_does_not_accumulate_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Reset must replace the sandbox, not add a second one on top."""
    clinic = await make_clinic(db_session)
    await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    after_first = await db_session.scalar(
        select(func.count()).select_from(Slot).where(Slot.clinic_id == clinic.id)
    )
    await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    after_second = await db_session.scalar(
        select(func.count()).select_from(Slot).where(Slot.clinic_id == clinic.id)
    )
    assert after_first == after_second


async def test_reset_preserves_the_clinic_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Reset restores the sandbox; it does not un-ingest the clinic."""
    clinic = await make_clinic(db_session)
    clinic.demo_phone = "+46812345678"
    clinic.profile_reviewed_by = "Fabian"
    await db_session.flush()

    await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    await db_session.refresh(clinic)

    assert clinic.demo_phone == "+46812345678"
    assert clinic.profile_reviewed_by == "Fabian"
    assert clinic.slug == "demo"
    assert clinic.seeded_at is not None
    assert clinic.seed_fingerprint is not None


async def test_reset_isolates_clinics(client: AsyncClient, db_session: AsyncSession) -> None:
    """Decision D5: two demo meetings on the same day must not be able to collide."""
    a = await make_clinic(db_session, slug="clinic-a")
    b = await make_clinic(db_session, slug="clinic-b")

    await client.post(f"/sim/{a.id}/reset", json=RESET_BODY)
    b_body = (await client.post(f"/sim/{b.id}/reset", json=RESET_BODY)).json()

    a_slots_before = await db_session.scalar(
        select(func.count()).select_from(Slot).where(Slot.clinic_id == a.id)
    )
    await client.post(f"/sim/{b.id}/reset", json=RESET_BODY)
    a_slots_after = await db_session.scalar(
        select(func.count()).select_from(Slot).where(Slot.clinic_id == a.id)
    )

    assert a_slots_before == a_slots_after
    await db_session.refresh(a)
    assert a.seed_fingerprint != b_body["fingerprint"]


async def test_unknown_clinic_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/sim/{uuid.uuid4()}/reset", json=RESET_BODY)
    assert resp.status_code == 404


# --- the QA gate ----------------------------------------------------------------------------


async def test_unreviewed_ingested_profile_cannot_be_demoed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """This is what makes spec §5's "obligatorisk QA-grind" actually mandatory."""
    clinic = await make_clinic(db_session, slug="scraped", source="ingestion")

    resp = await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "profile_not_reviewed"
    # The message must tell the operator exactly how to unblock themselves.
    assert "vakans-review approve scraped" in detail["message"]


async def test_reviewed_ingested_profile_can_be_demoed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.timeutils import now_utc

    clinic = await make_clinic(db_session, slug="scraped", source="ingestion")
    clinic.profile_reviewed_at = now_utc()
    clinic.profile_reviewed_by = "Fabian"
    await db_session.flush()

    resp = await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)
    assert resp.status_code == 200


async def test_hardcoded_profile_needs_no_review(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The bundled demo clinic is ours, so it seeds without ceremony."""
    clinic = await make_clinic(db_session, source="hardcoded")
    assert (await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)).status_code == 200


# --- listing and auth -------------------------------------------------------------------------


async def test_list_clinics_shows_seed_state(client: AsyncClient, db_session: AsyncSession) -> None:
    clinic = await make_clinic(db_session)
    await client.post(f"/sim/{clinic.id}/reset", json=RESET_BODY)

    rows = (await client.get("/sim/clinics")).json()
    assert len(rows) == 1
    assert rows[0]["slug"] == "demo"
    assert rows[0]["seeded_at"] is not None
    assert rows[0]["profile_source"] == "hardcoded"


async def test_demo_token_is_required_outside_local(
    db_session: AsyncSession, settings, monkeypatch
) -> None:
    """/sim is destructive: a curious clinic owner must not be able to reset a live demo."""
    from httpx import ASGITransport
    from httpx import AsyncClient as Client
    from pydantic import SecretStr

    from app.config import get_settings
    from app.db import get_session
    from app.main import create_app

    guarded = settings.model_copy(update={"env": "demo", "demo_token": SecretStr("s3cret")})
    app = create_app()

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: guarded

    clinic = await make_clinic(db_session)
    async with Client(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.get("/sim/clinics")).status_code == 401
        assert (
            await c.post(
                f"/sim/{clinic.id}/reset", json=RESET_BODY, headers={"X-Demo-Token": "wrong"}
            )
        ).status_code == 401
        ok = await c.post(
            f"/sim/{clinic.id}/reset", json=RESET_BODY, headers={"X-Demo-Token": "s3cret"}
        )
        assert ok.status_code == 200


async def test_healthz_reports_the_migration_revision(client: AsyncClient) -> None:
    body = (await client.get("/healthz")).json()
    assert body["database"] is True
    assert body["migration_revision"] == "0001"
    assert body["status"] == "ok"
