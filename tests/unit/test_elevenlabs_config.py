"""The generated ElevenLabs config.

Shapes here were taken from reading `@elevenlabs/cli` 0.5.6's own source
(`dist/tools/commands/impl.js`), not from their prose docs, after a push failed on a shape I
had inferred. These tests pin what that source showed.
"""

from __future__ import annotations

import uuid

from app.voice.elevenlabs import (
    agent_config,
    agents_registry,
    asr_keywords,
    tool_ids_from_registry,
    tool_payloads,
    tools_registry,
)

CLINIC = uuid.UUID("f0f79d7d-1f96-4bea-8363-019e6a374f68")
BASE = "https://example.trycloudflare.com"
NAMES = ["availability", "book", "cancel"]


def payloads():
    return tool_payloads(base_url=BASE, clinic_id=CLINIC)


# --- tool files ---------------------------------------------------------------------------


def test_a_tool_file_is_unwrapped() -> None:
    """Their CLI wraps it in `tool_config` itself when calling the API. Emitting the wrapper
    here made `tools push` read an unnamed, typeless tool."""
    book = next(p for p in payloads() if p["name"] == "book")

    assert "tool_config" not in book
    assert book["type"] == "webhook"
    assert set(book) >= {"name", "description", "type", "api_schema", "response_timeout_secs"}


def test_every_tool_points_at_this_clinic_and_carries_the_token() -> None:
    for payload in payloads():
        schema = payload["api_schema"]
        assert schema["url"].startswith(f"{BASE}/agent/{CLINIC}/")
        assert schema["method"] == "POST"
        # The token is a workspace-secret reference, never the literal value — these files are
        # generated into a repo.
        assert schema["request_headers"]["X-Agent-Token"] == "{{secret__agent_token}}"


def test_required_parameters_are_marked_required() -> None:
    book = next(p for p in payloads() if p["name"] == "book")
    schema = book["api_schema"]["request_body_schema"]

    assert set(schema["required"]) == {"slot_id", "service_id", "patient_name", "phone"}
    assert set(schema["properties"]) == set(schema["required"])


# --- registries: the id-preserving bit ------------------------------------------------------


def test_a_fresh_tools_registry_has_no_ids_so_push_creates() -> None:
    registry = tools_registry(slug="demo", tool_names=NAMES)

    assert [t["config"] for t in registry["tools"]] == [
        "tool_configs/demo-availability.json",
        "tool_configs/demo-book.json",
        "tool_configs/demo-cancel.json",
    ]
    assert all("id" not in t for t in registry["tools"])


def test_regenerating_keeps_the_ids_their_cli_wrote_back() -> None:
    """The bug this prevents: `make voice` runs every time the tunnel URL changes, and an entry
    without an id is a *create*. Dropping the ids would mint seven new tools per run."""
    after_push = {
        "tools": [
            {"type": "webhook", "config": "tool_configs/demo-availability.json", "id": "tool_a"},
            {"type": "webhook", "config": "tool_configs/demo-book.json", "id": "tool_b"},
            {"type": "webhook", "config": "tool_configs/demo-cancel.json", "id": "tool_c"},
        ]
    }

    regenerated = tools_registry(slug="demo", tool_names=NAMES, existing=after_push)

    assert [t["id"] for t in regenerated["tools"]] == ["tool_a", "tool_b", "tool_c"]


def test_a_new_tool_added_later_gets_no_id_and_is_created() -> None:
    existing = {"tools": [{"config": "tool_configs/demo-book.json", "id": "tool_b"}]}

    regenerated = tools_registry(slug="demo", tool_names=NAMES, existing=existing)

    by_config = {t["config"]: t for t in regenerated["tools"]}
    assert by_config["tool_configs/demo-book.json"]["id"] == "tool_b"
    assert "id" not in by_config["tool_configs/demo-availability.json"]


def test_the_agent_id_in_the_file_beats_the_one_in_env() -> None:
    """`agents add` writes the real id here. .env may be stale or empty; the file is what the
    CLI actually did."""
    existing = {"agents": [{"config": "agent_configs/demo.json", "id": "agent_real"}]}

    registry = agents_registry(slug="demo", agent_id="agent_stale", existing=existing)

    assert registry["agents"][0]["id"] == "agent_real"


def test_the_agent_id_falls_back_to_env_before_the_first_push() -> None:
    registry = agents_registry(slug="demo", agent_id="agent_from_env")
    assert registry["agents"][0]["id"] == "agent_from_env"


def test_no_id_anywhere_means_no_id_key() -> None:
    """`agents add` refuses an entry that claims an id it cannot find."""
    assert "id" not in agents_registry(slug="demo", agent_id=None)["agents"][0]


def test_tool_ids_are_read_back_for_the_agent() -> None:
    registry = {"tools": [{"id": "a"}, {"config": "x.json"}, {"id": "b"}]}
    assert tool_ids_from_registry(registry) == ["a", "b"]
    assert tool_ids_from_registry(None) == []


# --- the agent config ------------------------------------------------------------------------


def config(**kwargs):
    return agent_config(
        clinic_name="Rörelsekliniken",
        prompt="…",
        first_message="Hej!",
        llm="claude-sonnet-4-5",
        keywords=["naprapat"],
        tool_ids=["tool_a"],
        **kwargs,
    )


def test_the_agent_references_its_tools() -> None:
    assert config()["conversation_config"]["agent"]["prompt"]["tool_ids"] == ["tool_a"]


def test_the_ai_disclosure_cannot_be_talked_over() -> None:
    """EU AI Act art. 50. Interruptible, an eager caller talks over it and it did not happen."""
    assert config()["conversation_config"]["agent"]["disable_first_message_interruptions"] is True


def test_the_primary_model_is_not_its_own_fallback() -> None:
    backup = config()["conversation_config"]["agent"]["prompt"]["backup_llm_config"]
    assert "claude-sonnet-4-5" not in backup["order"]
    assert backup["preference"] == "override"


def test_numbers_are_normalised_after_the_model_not_by_it() -> None:
    """The default, `system_prompt`, asks the model to rewrite numbers before speaking — which
    means it edits our prices on the way out."""
    assert config()["conversation_config"]["tts"]["text_normalisation_type"] == "elevenlabs"


def test_the_voice_is_never_generated() -> None:
    """Chosen by listening. A deploy must not overwrite an ear."""
    assert "voice_id" not in config()["conversation_config"]["tts"]


# --- ASR keywords ---------------------------------------------------------------------------


def test_keywords_include_practitioners_whole_and_split() -> None:
    """Callers say "Johanna" far more often than "Johanna Lindqvist"."""
    words = asr_keywords(
        clinic_name="Rörelsekliniken Vasastan",
        service_names=["Nybesök naprapati"],
        staff_names=["Johanna Lindqvist"],
    )

    assert "Johanna Lindqvist" in words
    assert "Johanna" in words
    assert "naprapati" in words


def test_keywords_drop_noise() -> None:
    """Stop words and stray digits would dilute the boost."""
    words = asr_keywords(clinic_name="K", service_names=["Massage 60 min och rygg"], staff_names=[])

    assert "och" not in words
    assert "60" not in words
    assert "K" not in words  # too short to be worth boosting
    assert "Massage" in words
