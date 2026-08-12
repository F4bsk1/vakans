"""The clinic_profile contract (spec §5) and the bundled demo clinic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.fixtures import load_demo_profile, load_demo_profile_document
from app.schemas.clinic_profile import (
    BookingSystemHint,
    ClinicProfile,
    FaqItem,
    ServiceItem,
)


def test_demo_profile_loads_and_is_complete() -> None:
    p = load_demo_profile()
    assert p.name == "Rörelsekliniken Vasastan"
    assert p.booking_system_hint is BookingSystemHint.KADDIO
    assert len(p.staff) == 4
    assert len(p.services) == 8
    assert len(p.faq) == 6
    assert p.policies.cancellation
    # Every service is priced, so the SEK counter in the refill demo has real numbers.
    assert len(p.priced_services()) == 8


def test_demo_profile_median_duration_drives_slot_length() -> None:
    # Durations are 45/30/45/30/60/60/30/30 -> median 37.5 -> 37. The seed uses this as the
    # sandbox slot length, so a change to the fixture visibly changes the calendar.
    assert load_demo_profile().median_duration() == 37


def test_median_duration_falls_back_when_nothing_published() -> None:
    p = ClinicProfile(name="Tom klinik", services=[ServiceItem(name="Behandling")])
    assert p.median_duration() == 45


def test_spec_json_roundtrip_keeps_the_wrapper() -> None:
    # Artifacts on disk must match spec §5's {"clinic_profile": {...}} shape exactly.
    p = load_demo_profile()
    spec = p.to_spec_json()
    assert set(spec) == {"clinic_profile"}
    assert ClinicProfile.from_spec_json(spec) == p


def test_compact_strips_provenance_and_unbookable_services() -> None:
    p = ClinicProfile(
        name="K",
        services=[
            ServiceItem(
                name="Nybesök",
                price_sek=895,
                price_source_url="https://k.example/priser",
                price_evidence="Nybesök 895 kr",
            ),
            ServiceItem(name="Internt", bookable=False),
        ],
    )
    compact = p.compact()
    assert [s["name"] for s in compact["services"]] == ["Nybesök"]
    # Provenance is for the human QA gate; every prompt token costs latency on a live call.
    assert "price_evidence" not in compact["services"][0]
    assert "price_source_url" not in compact["services"][0]


def test_all_prices_have_sources_is_false_without_evidence() -> None:
    p = ClinicProfile(name="K", services=[ServiceItem(name="Nybesök", price_sek=895)])
    assert p.all_prices_have_sources() is False

    p.services[0].price_source_url = "https://k.example/priser"
    p.services[0].price_evidence = "Nybesök 895 kr"
    assert p.all_prices_have_sources() is True


def test_unpriced_services_do_not_require_evidence() -> None:
    p = ClinicProfile(name="K", services=[ServiceItem(name="Konsultation")])
    assert p.all_prices_have_sources() is True


def test_extra_keys_are_rejected() -> None:
    # extra="forbid" is deliberate: under structured extraction an invented key should be a
    # loud failure that triggers a retry, not data silently dropped.
    with pytest.raises(ValidationError):
        ClinicProfile.model_validate({"name": "K", "hallucinated_field": "nope"})


def test_faq_requires_a_source_url() -> None:
    # Spec §5: "varje faq-post ska ha source_url".
    with pytest.raises(ValidationError):
        FaqItem.model_validate({"q": "Kostar det?", "a": "Ja."})


def test_negative_price_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ServiceItem(name="Nybesök", price_sek=-100)


def test_demo_document_is_hardcoded_and_unreviewed() -> None:
    doc = load_demo_profile_document()
    assert doc.meta.source == "hardcoded"
    # Not reviewed, but the reset gate only applies to ingestion-sourced profiles.
    assert doc.is_reviewed is False
