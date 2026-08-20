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
    assert "MEDICINSK GRÄNS" in prompt
    assert "1177" in prompt
    assert "bröstsmärta" in prompt
    assert "escalate" in prompt


def test_system_prompt_forbids_inventing_prices() -> None:
    """Same rule scrub_unverified enforces on ingestion, stated to the agent."""
    prompt = build_system_prompt(load_demo_profile())
    assert "Hitta ALDRIG på priser" in prompt


def test_system_prompt_requires_identity_check_before_touching_a_booking() -> None:
    prompt = build_system_prompt(load_demo_profile())
    assert "namn och det" in prompt and "telefonnummer" in prompt


def test_system_prompt_embeds_the_clinics_real_data() -> None:
    prompt = build_system_prompt(load_demo_profile())
    assert "Johanna Lindqvist" in prompt
    assert "895" in prompt
    assert "Rörelsekliniken Vasastan" in prompt


def test_system_prompt_strips_ingestion_provenance() -> None:
    """price_evidence exists for the human QA gate; every prompt token costs call latency."""
    prompt = build_system_prompt(load_demo_profile())
    assert "price_evidence" not in prompt
    assert "price_source_url" not in prompt


def test_system_prompt_tells_the_agent_to_read_speech_verbatim() -> None:
    """The whole latency trick collapses if the model re-renders times and prices itself."""
    prompt = build_system_prompt(load_demo_profile())
    assert "ordagrant" in prompt
