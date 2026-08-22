"""The medical boundary. Charter §2 calls this a product line, not a nice-to-have."""

from __future__ import annotations

import pytest

from app.agent import safety
from app.agent.prompt import GREETING_TEMPLATE, build_greeting, build_system_prompt
from app.fixtures import load_demo_profile


@pytest.mark.parametrize(
    "utterance",
    [
        "jag har ont i bröstet",
        "Jag har bröstsmärtor sedan i morse",
        "det känns som ett tryck över bröstet",
        "jag har andnöd",
        "har svårt att andas",
        "jag får inte luft",
        "min man har svimmat",
        "hon svimmade nyss",
        "det är en kraftig blödning",
        "han blöder mycket",
        "ansiktet hänger och han släpar på orden",
        "jag tror det är en hjärtinfarkt",
        "jag är svullen i halsen efter en geting",
        "jag vill inte leva längre",
    ],
)
def test_emergencies_are_caught(utterance: str) -> None:
    verdict = safety.check(utterance)
    assert verdict.flag is safety.RedFlag.EMERGENCY, utterance
    assert "112" in verdict.speech
    assert "1177" in verdict.speech


@pytest.mark.parametrize(
    "utterance",
    [
        "jag har ont i ryggen, vad tror du att det är?",
        "är det farligt att det gör ont när jag böjer mig?",
        "bör jag oroa mig för det här?",
        "vilken behandling behöver jag?",
        "kan jag ta Alvedon före besöket?",
        "får jag träna med det här?",
        "vad har jag för skada?",
    ],
)
def test_advice_requests_are_declined(utterance: str) -> None:
    verdict = safety.check(utterance)
    assert verdict.flag is safety.RedFlag.ADVICE, utterance
    assert "1177" in verdict.speech
    # An advice request is not an emergency — sending everyone to 112 would be crying wolf.
    assert "112" not in verdict.speech


def test_emergency_beats_advice() -> None:
    """ "Jag har ont i bröstet, vad tror du?" is 112, not "I can't assess that"."""
    verdict = safety.check("jag har ont i bröstet, vad tror du att det är?")
    assert verdict.flag is safety.RedFlag.EMERGENCY


@pytest.mark.parametrize(
    "utterance",
    [
        "jag vill boka en tid för massage",
        "har ni öppet på lördagar?",
        "vad kostar ett nybesök?",
        "kan jag parkera i närheten?",
        "jag heter Anna Andersson",
        "jag vill omboka min tid till nästa vecka",
        "",
        None,
    ],
)
def test_ordinary_reception_traffic_passes(utterance: str | None) -> None:
    assert safety.check(utterance).flag is None


def test_check_scans_every_supplied_field() -> None:
    """Red flags arrive in whichever field the model happened to fill in."""
    assert safety.check("massage", None, "jag har andnöd").flag is safety.RedFlag.EMERGENCY


def test_verdict_reports_what_matched() -> None:
    """The dashboard escalation panel shows this, so it has to be specific."""
    verdict = safety.check("kraftig blödning och andnöd")
    assert set(verdict.matched) == {"blödning", "andnöd"}


def test_clear_verdict_has_no_speech() -> None:
    assert safety.CLEAR.speech == ""
    assert safety.CLEAR.is_blocked is False


# --- the prompt --------------------------------------------------------------------------------


def test_greeting_discloses_the_ai_verbatim() -> None:
    """EU AI Act art. 50. The charter calls this non-negotiable."""
    greeting = build_greeting(load_demo_profile())
    assert "digitala assistent" in greeting
    assert "Rörelsekliniken Vasastan" in greeting
    # Exactly the spec §4 wording — not a paraphrase.
    assert greeting == GREETING_TEMPLATE.format(clinic_name="Rörelsekliniken Vasastan")


def test_recording_disclosure_is_part_of_the_greeting() -> None:
    """Charter D6: say it before anything is recorded, not in the small print."""
    greeting = build_greeting(load_demo_profile(), recording=True)
    assert "spelas in" in greeting
    assert "sju dagar" in greeting


def test_system_prompt_carries_the_medical_boundary() -> None:
    prompt = build_system_prompt(load_demo_profile())
    assert "bedömer aldrig symtom" in prompt
    assert "escalate" in prompt
    for red_flag in ("bröstsmärta", "andnöd", "svimning", "blödning"):
        assert red_flag in prompt


def test_the_caller_is_referred_to_1177_by_the_tool_not_the_prompt() -> None:
    """1177 used to be a line in the prompt, which made it dependent on the model repeating it.

    It now lives in `safety`'s speech, which `escalate` returns and the agent reads verbatim —
    so the referral happens whether or not the model was paying attention.
    """
    assert "1177" in safety.ADVICE_SPEECH
    assert "1177" in safety.EMERGENCY_SPEECH
    assert "1177" not in build_system_prompt(load_demo_profile())


def test_system_prompt_forbids_inventing_prices() -> None:
    """Same rule scrub_unverified enforces on ingestion, stated to the agent."""
    prompt = build_system_prompt(load_demo_profile())
    assert "hittat på det" in prompt


def test_system_prompt_requires_identity_check_before_touching_a_booking() -> None:
    prompt = build_system_prompt(load_demo_profile())
    assert "namn och det" in prompt and "telefonnummer" in prompt


def test_system_prompt_contains_no_clinic_facts() -> None:
    """The agent is told nothing it could state without calling a tool.

    A model that was never given a price is far less likely to invent one — and when it does
    say a price, you can hear that it skipped the tool. This is the same principle as
    scrub_unverified on ingestion: do not rely on the model choosing well.
    """
    prompt = build_system_prompt(load_demo_profile())
    for fact in ("895", "695", "Johanna Lindqvist", "Marcus Ohlsson", "08:00", "24 timmar"):
        assert fact not in prompt, f"{fact!r} leaked into the prompt"


def test_system_prompt_keeps_what_it_needs_to_route() -> None:
    """Service names stay: the agent must tell "en tid för massage" from "hos naprapaten",
    and answer "vad erbjuder ni?" without a round trip."""
    prompt = build_system_prompt(load_demo_profile())
    assert "Rörelsekliniken Vasastan" in prompt
    assert "Klassisk massage 60 min" in prompt
    assert "Nybesök fysioterapi" in prompt


def test_system_prompt_says_the_facts_are_not_there() -> None:
    """Without this the model treats an empty profile as "nothing to offer" rather than
    "call a tool"."""
    prompt = build_system_prompt(load_demo_profile())
    assert "inga uppgifter" in prompt
    assert "hittat på det" in prompt


def test_system_prompt_strips_ingestion_provenance() -> None:
    """price_evidence exists for the human QA gate; every prompt token costs call latency."""
    prompt = build_system_prompt(load_demo_profile())
    assert "price_evidence" not in prompt
    assert "price_source_url" not in prompt


def test_system_prompt_tells_the_agent_to_read_speech_verbatim() -> None:
    """The whole latency trick collapses if the model re-renders times and prices itself."""
    prompt = build_system_prompt(load_demo_profile())
    assert "ordagrant" in prompt


def test_the_opening_discloses_that_the_caller_is_talking_to_an_ai() -> None:
    """EU AI Act art. 50. It is set as ElevenLabs' `first_message` too, but it stays in the
    prompt as well so clearing that field in their dashboard cannot silently drop it."""
    profile = load_demo_profile()
    greeting = build_greeting(profile)

    assert "digitala assistent" in greeting
    assert greeting in build_system_prompt(profile)


def test_the_prompt_stays_short() -> None:
    """Every character is processed on every turn of a call with a 1.5-second budget.

    The ceiling is generous. It exists to catch a regression back to the 3,126-character
    version with its numbered rule list and five-step booking flowchart — which is what made
    the agent sound like a phone menu, and which the tool descriptions made unnecessary.
    """
    assert len(build_system_prompt(load_demo_profile())) < 2000
