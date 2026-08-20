"""Merging chunk extractions, and the evidence check that deletes invented facts.

`scrub_unverified` is the single most valuable function in the ingestion pipeline. Spec §5:
"Fel pris i demo = död affär."
"""

from __future__ import annotations

import pytest

from app.schemas.clinic_profile import (
    ClinicProfile,
    FaqItem,
    OpeningHours,
    Policies,
    Practical,
    ServiceItem,
    StaffMember,
    Weekday,
)
from ingestion.crawl import CrawlResult, Page
from ingestion.extract import PartialProfile
from ingestion.merge import (
    merge_partials,
    normalize_person_name,
    normalize_service_name,
    quote_is_present,
    scrub_unverified,
)

URL = "https://klinik.example/priser"
PAGE_TEXT = (
    "Prislista\nNybesök fysioterapi 895 kr, 45 minuter.\n"
    "Återbesök 695 kr.\nIdrottsmedicinsk undersökning 1 195 kr.\n"
    "Vår personal: Johanna Lindqvist, leg. fysioterapeut."
)


def _crawl(pages: dict[str, str] | None = None, *, skipped=None) -> CrawlResult:
    pages = pages or {URL: PAGE_TEXT}
    return CrawlResult(
        base_url="https://klinik.example",
        pages=[
            Page(url=u, final_url=u, depth=0, title=None, text=t, word_count=len(t.split()))
            for u, t in pages.items()
        ],
        skipped=skipped or [],
        robots_txt=None,
        sitemap_urls=[],
        started_at=None,
        finished_at=None,
    )


def _service(name: str, price: float | None, evidence: str | None, url: str | None = URL):
    return ServiceItem(name=name, price_sek=price, price_evidence=evidence, price_source_url=url)


# --- the evidence check ------------------------------------------------------------------------


def test_a_fabricated_price_is_deleted_and_reported() -> None:
    """The whole point. The model claims 1495 kr with a quote that appears nowhere."""
    profile = ClinicProfile(
        name="Klinik", services=[_service("Nybesök", 1495, "Nybesök fysioterapi 1495 kr")]
    )
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert scrubbed.services[0].price_sek is None
    assert scrubbed.services[0].price_evidence is None
    assert any("borttaget" in w and "Nybesök" in w for w in warnings)


def test_a_real_price_with_a_real_quote_survives() -> None:
    profile = ClinicProfile(
        name="Klinik", services=[_service("Nybesök", 895, "Nybesök fysioterapi 895 kr")]
    )
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert scrubbed.services[0].price_sek == 895
    assert not any("borttaget" in w for w in warnings)


def test_a_price_without_any_quote_is_deleted() -> None:
    profile = ClinicProfile(name="Klinik", services=[_service("Nybesök", 895, None)])
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert scrubbed.services[0].price_sek is None
    assert any("inget källcitat" in w for w in warnings)


def test_a_digit_transposition_does_not_slip_through() -> None:
    """859 vs 895 is exactly the error that would survive a fuzzy match."""
    profile = ClinicProfile(
        name="Klinik", services=[_service("Nybesök", 859, "Nybesök fysioterapi 859 kr")]
    )
    scrubbed, _ = scrub_unverified(profile, _crawl())
    assert scrubbed.services[0].price_sek is None


def test_a_price_quoted_from_another_crawled_page_is_kept_but_flagged() -> None:
    """Models often cite a sibling URL while quoting genuine text. Keep the fact, flag the cite."""
    profile = ClinicProfile(
        name="Klinik",
        services=[
            _service("Nybesök", 895, "Nybesök fysioterapi 895 kr", "https://klinik.example/x")
        ],
    )
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert scrubbed.services[0].price_sek == 895
    assert any("källadressen stämde inte" in w for w in warnings)


def test_prices_written_with_thousand_separators_verify() -> None:
    """Swedish sites write "1 195 kr", sometimes with a non-breaking space."""
    profile = ClinicProfile(
        name="Klinik",
        services=[_service("Idrottsmedicin", 1195, "Idrottsmedicinsk undersökning 1 195 kr")],
    )
    scrubbed, _ = scrub_unverified(profile, _crawl())
    assert scrubbed.services[0].price_sek == 1195


def test_unpriced_services_pass_through_untouched() -> None:
    profile = ClinicProfile(name="Klinik", services=[ServiceItem(name="Konsultation")])
    scrubbed, warnings = scrub_unverified(profile, _crawl())
    assert scrubbed.services[0].name == "Konsultation"
    assert not any("borttaget" in w for w in warnings)


def test_faq_citing_an_uncrawled_url_is_dropped() -> None:
    profile = ClinicProfile(
        name="Klinik",
        faq=[
            FaqItem(q="Behöver jag remiss?", a="Nej.", source_url=URL),
            FaqItem(q="Påhittat?", a="Ja.", source_url="https://klinik.example/aldrig-hamtad"),
        ],
    )
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert [f.q for f in scrubbed.faq] == ["Behöver jag remiss?"]
    assert any("FAQ borttagen" in w for w in warnings)


def test_a_fabricated_practitioner_is_removed() -> None:
    """An invented practitioner is as damaging as an invented price: the agent will offer
    appointments with someone who does not work there."""
    profile = ClinicProfile(
        name="Klinik",
        staff=[
            StaffMember(name="Johanna Lindqvist", title="Leg. fysioterapeut"),
            StaffMember(name="Påhittad Person", title="Leg. naprapat"),
        ],
    )
    scrubbed, warnings = scrub_unverified(profile, _crawl())

    assert [s.name for s in scrubbed.staff] == ["Johanna Lindqvist"]
    assert any("Behandlare borttagen" in w for w in warnings)


def test_wrong_site_guard_fires_when_the_name_is_absent() -> None:
    profile = ClinicProfile(name="Helt Annan Klinik")
    _, warnings = scrub_unverified(profile, _crawl())
    assert any("rätt webbplats" in w for w in warnings)


def test_skipped_pdfs_are_surfaced_to_the_reviewer() -> None:
    """Silently skipping a prislista.pdf is how you demo without prices."""
    crawl = _crawl(skipped=[("https://klinik.example/prislista.pdf", "pdf")])
    _, warnings = scrub_unverified(ClinicProfile(name="Klinik"), crawl)
    assert any("PDF" in w and "prislista.pdf" in w for w in warnings)


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("Nybesök fysioterapi 895 kr", True),
        ("nybesök   FYSIOTERAPI 895 KR", True),  # whitespace and case insensitive
        ("Nybesök fysioterapi 894 kr", False),
        ("", False),
        (None, False),
    ],
)
def test_quote_is_present(quote: str | None, expected: bool) -> None:
    assert quote_is_present(quote, PAGE_TEXT) is expected


# --- merging -----------------------------------------------------------------------------------


def test_services_are_unioned_by_normalised_name() -> None:
    a = PartialProfile(services=[ServiceItem(name="Nybesök  (45 min)")])
    b = PartialProfile(services=[ServiceItem(name="nybesök")])
    assert len(merge_partials([a, b]).services) == 1


def test_the_priced_variant_of_a_service_wins() -> None:
    a = PartialProfile(services=[ServiceItem(name="Nybesök")])
    b = PartialProfile(services=[_service("Nybesök", 895, "Nybesök 895 kr")])
    merged = merge_partials([a, b])
    assert merged.services[0].price_sek == 895


def test_staff_are_deduped_preferring_the_entry_with_a_title() -> None:
    a = PartialProfile(staff=[StaffMember(name="Johanna Lindqvist")])
    b = PartialProfile(staff=[StaffMember(name="johanna lindqvist", title="Leg. fysioterapeut")])
    merged = merge_partials([a, b])
    assert len(merged.staff) == 1
    assert merged.staff[0].title == "Leg. fysioterapeut"


def test_scalars_use_the_most_frequent_value() -> None:
    partials = [
        PartialProfile(name="Rörelsekliniken", phone="08-123 45 67"),
        PartialProfile(name="Rörelsekliniken", phone="08-123 45 67"),
        PartialProfile(name="Rörelsekliniken Vasastan", phone="08-999 99 99"),
    ]
    merged = merge_partials(partials)
    assert merged.name == "Rörelsekliniken"
    assert merged.phone == "08-123 45 67"


def test_the_most_complete_opening_hours_set_wins() -> None:
    partial_hours = [OpeningHours(day=Weekday.MON, open="08:00", close="17:00")]
    full_hours = [OpeningHours(day=d, open="08:00", close="17:00") for d in list(Weekday)[:5]]
    merged = merge_partials(
        [PartialProfile(opening_hours=partial_hours), PartialProfile(opening_hours=full_hours)]
    )
    assert len(merged.opening_hours) == 5


def test_faq_is_deduped_by_question() -> None:
    item = FaqItem(q="Behöver jag remiss?", a="Nej.", source_url=URL)
    merged = merge_partials([PartialProfile(faq=[item]), PartialProfile(faq=[item])])
    assert len(merged.faq) == 1


def test_policies_take_the_first_non_empty_field_across_chunks() -> None:
    merged = merge_partials(
        [
            PartialProfile(policies=Policies(cancellation="24 timmar")),
            PartialProfile(policies=Policies(late_fee="Full avgift")),
        ]
    )
    assert merged.policies.cancellation == "24 timmar"
    assert merged.policies.late_fee == "Full avgift"


def test_insurance_lists_are_unioned() -> None:
    merged = merge_partials(
        [
            PartialProfile(policies=Policies(insurance_accepted=["If", "Skandia"])),
            PartialProfile(policies=Policies(insurance_accepted=["skandia", "Euro Accident"])),
        ]
    )
    assert len(merged.policies.insurance_accepted) == 3


def test_confidence_notes_are_concatenated_without_duplicates() -> None:
    merged = merge_partials(
        [
            PartialProfile(confidence_notes="Oklart om moms ingår."),
            PartialProfile(confidence_notes="Oklart om moms ingår."),
            PartialProfile(confidence_notes="Personal kan ha slutat."),
        ]
    )
    assert merged.confidence_notes.count("Oklart") == 1
    assert "Personal kan ha slutat." in merged.confidence_notes


def test_practical_fields_survive_the_merge() -> None:
    merged = merge_partials([PartialProfile(practical=Practical(parking="Garage i kvarteret"))])
    assert merged.practical.parking == "Garage i kvarteret"


def test_merging_nothing_yields_a_valid_empty_profile() -> None:
    assert merge_partials([]).name == "Okänd klinik"


def test_blank_service_and_staff_names_are_ignored() -> None:
    merged = merge_partials(
        [PartialProfile(services=[ServiceItem(name="   ")], staff=[StaffMember(name="")])]
    )
    assert merged.services == []
    assert merged.staff == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Massage (60 min)", "massage"),
        ("Nybesök – 45 min", "nybesök"),
        ("Nybesök  fysioterapi", "nybesök fysioterapi"),
    ],
)
def test_normalize_service_name(raw: str, expected: str) -> None:
    assert normalize_service_name(raw) == expected


def test_normalize_person_name_strips_leading_titles() -> None:
    assert normalize_person_name("Leg. Johanna Lindqvist") == "johanna lindqvist"
    assert normalize_person_name("Johanna  Lindqvist") == "johanna lindqvist"
