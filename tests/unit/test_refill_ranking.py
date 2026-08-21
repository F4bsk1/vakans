"""Candidate ranking and reply parsing — the two pure pieces of the refill cascade."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.refill.engine import Answer, CandidateInput, parse_reply, rank_candidates
from app.refill.messages import offer_sms, window_sv
from app.timeutils import DEFAULT_TZ

NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def candidate(
    *,
    days_waited: float = 1.0,
    risk: float = 0.1,
    exact: bool = True,
    waitlist_id: UUID | None = None,
    name: str = "Anna",
) -> CandidateInput:
    return CandidateInput(
        waitlist_id=waitlist_id or uuid4(),
        patient_id=uuid4(),
        service_id=uuid4(),
        name=name,
        phone="+46701234567",
        created_at=NOW - timedelta(days=days_waited),
        risk=risk,
        exact_service=exact,
    )


def test_longer_wait_ranks_first_when_risk_is_equal() -> None:
    short = candidate(days_waited=1, name="kort")
    long = candidate(days_waited=14, name="lang")

    ranked = rank_candidates([short, long], now=NOW)

    assert [c.input.name for c in ranked] == ["lang", "kort"]


def test_lower_risk_ranks_first_when_wait_is_equal() -> None:
    """Spec §5: "lågriskpatient först". Refilling a slot with someone who will not show up
    fixes the number and not the empty chair."""
    risky = candidate(risk=0.6, name="risk")
    safe = candidate(risk=0.05, name="trygg")

    ranked = rank_candidates([risky, safe], now=NOW)

    assert [c.input.name for c in ranked] == ["trygg", "risk"]


def test_an_exact_service_match_beats_a_long_wait_on_a_compatible_service() -> None:
    """The bonus is deliberately larger than both weighted terms combined."""
    exact_but_new = candidate(days_waited=0.1, risk=0.4, exact=True, name="exakt")
    compatible_veteran = candidate(days_waited=60, risk=0.0, exact=False, name="kompatibel")

    ranked = rank_candidates([compatible_veteran, exact_but_new], now=NOW)

    assert [c.input.name for c in ranked] == ["exakt", "kompatibel"]


def test_ranking_is_stable_for_identical_candidates() -> None:
    """Two entries seeded in the same second must not reorder between runs, or "same seed,
    same demo" stops being true."""
    a = candidate(waitlist_id=UUID(int=1), name="a")
    b = candidate(waitlist_id=UUID(int=2), name="b")

    assert [c.input.name for c in rank_candidates([a, b], now=NOW)] == ["a", "b"]
    assert [c.input.name for c in rank_candidates([b, a], now=NOW)] == ["a", "b"]


def test_wait_term_saturates_rather_than_growing_without_bound() -> None:
    """Someone who has waited a year should not outrank everyone on wait time alone forever."""
    a_year = candidate(days_waited=365, risk=0.5)
    three_weeks = candidate(days_waited=21, risk=0.5)

    ranked = rank_candidates([a_year, three_weeks], now=NOW)

    assert ranked[0].score == ranked[1].score


def test_a_negative_wait_does_not_produce_a_negative_score() -> None:
    """A waitlist entry created "in the future" is a clock skew, not a bonus."""
    future = candidate(days_waited=-5, risk=0.0)
    assert rank_candidates([future], now=NOW)[0].score >= 0


# --- reply parsing ------------------------------------------------------------------------------


def test_plain_yes_and_no() -> None:
    assert parse_reply("JA") is Answer.ACCEPT
    assert parse_reply("ja") is Answer.ACCEPT
    assert parse_reply("Nej") is Answer.DECLINE


def test_yes_with_swedish_politeness() -> None:
    """ "Ja tack!" is the single most likely real reply and must not read as unknown."""
    assert parse_reply("Ja tack!") is Answer.ACCEPT
    assert parse_reply("  ja, den passar bra  ") is Answer.ACCEPT
    assert parse_reply("Nej tack") is Answer.DECLINE


def test_a_no_buried_in_a_sentence_is_not_a_no() -> None:
    """Only the first word decides. Guessing at prose over SMS books the wrong people in."""
    assert parse_reply("jag vet inte, nej förresten kanske") is Answer.UNKNOWN
    assert parse_reply("Kan jag få en annan tid?") is Answer.UNKNOWN


def test_empty_and_missing_bodies_are_unknown() -> None:
    assert parse_reply(None) is Answer.UNKNOWN
    assert parse_reply("") is Answer.UNKNOWN
    assert parse_reply("   ") is Answer.UNKNOWN


def test_stop_counts_as_a_decline() -> None:
    """Carriers treat STOPP as opt-out; the cascade must at minimum stop asking."""
    assert parse_reply("STOPP") is Answer.DECLINE


# --- the message itself -------------------------------------------------------------------------


def test_the_offer_states_the_window_the_engine_actually_honours() -> None:
    assert window_sv(timedelta(seconds=90)) == "90 sekunder"
    assert window_sv(timedelta(seconds=120)) == "2 minuter"
    assert window_sv(timedelta(seconds=60)) == "en minut"


def test_offer_sms_names_what_when_and_the_one_word_to_reply() -> None:
    body = offer_sms(
        clinic_name="Rörelsekliniken",
        patient_name="Anna Andersson",
        service_name="Nybesök naprapat",
        starts_at=NOW + timedelta(days=1),
        resource_name="Johanna Lindqvist",
        tz=DEFAULT_TZ,
        now=NOW,
        timeout=timedelta(seconds=90),
    )

    assert body.startswith("Hej Anna!")  # first name only, like a receptionist
    assert "Johanna" in body and "Lindqvist" not in body
    assert "Svara JA" in body
    assert "90 sekunder" in body
    # One GSM-7 segment, so a cascade to nine people costs nine texts and not eighteen.
    assert len(body) <= 160
