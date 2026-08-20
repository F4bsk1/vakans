"""The QA gate rules, and the CLI's exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schemas.clinic_profile import (
    BookingSystemHint,
    ClinicProfile,
    ClinicProfileDocument,
    FaqItem,
    OpeningHours,
    Policies,
    ProfileMeta,
    ServiceItem,
    StaffMember,
    Weekday,
)
from app.timeutils import now_utc
from ingestion.validate import blocking, is_approvable, validate_profile, warnings
from tests.unit.test_merge import PAGE_TEXT, URL, _crawl


def _good_profile(**overrides) -> ClinicProfile:
    defaults = dict(
        name="Klinik",
        phone="08-123 45 67",
        booking_system_hint=BookingSystemHint.KADDIO,
        opening_hours=[OpeningHours(day=Weekday.MON, open="08:00", close="17:00")],
        staff=[StaffMember(name="Johanna Lindqvist", title="Leg. fysioterapeut")],
        services=[
            ServiceItem(
                name="Nybesök fysioterapi",
                price_sek=895,
                price_evidence="Nybesök fysioterapi 895 kr",
                price_source_url=URL,
            )
        ],
        policies=Policies(cancellation="24 timmar"),
        faq=[FaqItem(q="Remiss?", a="Nej.", source_url=URL)],
    )
    return ClinicProfile(**{**defaults, **overrides})


def _doc(profile: ClinicProfile | None = None, **meta) -> ClinicProfileDocument:
    return ClinicProfileDocument(
        profile=profile or _good_profile(),
        meta=ProfileMeta(source="ingestion", **meta),
    )


def _crawl_with_name() -> object:
    return _crawl({URL: PAGE_TEXT + "\nKlinik i Stockholm."})


# --- blocking rules ------------------------------------------------------------------------


def test_a_complete_profile_is_approvable() -> None:
    issues = validate_profile(_doc(), _crawl_with_name())
    assert is_approvable(issues), [i.message for i in blocking(issues)]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"name": ""}, "no_name"),
        ({"phone": None}, "no_phone"),
        ({"services": []}, "no_services"),
        ({"staff": []}, "no_staff"),
        ({"opening_hours": []}, "no_hours"),
    ],
)
def test_missing_essentials_block(override: dict, code: str) -> None:
    issues = validate_profile(_doc(_good_profile(**override)), _crawl_with_name())
    assert code in {i.code for i in blocking(issues)}


def test_a_price_without_evidence_blocks_approval() -> None:
    """Belt and braces: scrub_unverified should already have removed it."""
    profile = _good_profile(services=[ServiceItem(name="Nybesök", price_sek=895)])
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "price_without_evidence" in {i.code for i in blocking(issues)}


def test_wrong_site_guard_blocks() -> None:
    profile = _good_profile(name="Helt Annan Klinik")
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "name_not_in_source" in {i.code for i in blocking(issues)}


def test_no_crawl_means_no_wrong_site_check() -> None:
    """A hand-edited profile with no crawl on disk must still be checkable."""
    issues = validate_profile(_doc(), None)
    assert "name_not_in_source" not in {i.code for i in issues}


# --- warnings ------------------------------------------------------------------------------


def test_unknown_booking_system_warns_but_does_not_block() -> None:
    profile = _good_profile(booking_system_hint=BookingSystemHint.CUSTOM)
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "booking_system_unknown" in {i.code for i in warnings(issues)}
    assert is_approvable(issues)


def test_missing_cancellation_policy_warns() -> None:
    profile = _good_profile(policies=Policies())
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "no_cancellation_policy" in {i.code for i in warnings(issues)}


def test_an_implausibly_low_price_warns() -> None:
    """The "1 195 kr" read as "1" failure mode."""
    profile = _good_profile(
        services=[
            ServiceItem(
                name="Nybesök", price_sek=1, price_evidence="1 195 kr", price_source_url=URL
            )
        ]
    )
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "price_implausibly_low" in {i.code for i in warnings(issues)}


def test_an_implausibly_high_price_warns() -> None:
    profile = _good_profile(
        services=[
            ServiceItem(
                name="Paket", price_sek=12000, price_evidence="12000 kr", price_source_url=URL
            )
        ]
    )
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "price_implausibly_high" in {i.code for i in warnings(issues)}


def test_over_extraction_warns() -> None:
    services = [
        ServiceItem(name=f"Tjänst {i}", price_sek=500, price_evidence="x", price_source_url=URL)
        for i in range(30)
    ]
    issues = validate_profile(_doc(_good_profile(services=services)), _crawl_with_name())
    assert "too_many_services" in {i.code for i in warnings(issues)}


def test_duplicate_staff_warns() -> None:
    profile = _good_profile(
        staff=[StaffMember(name="Johanna Lindqvist"), StaffMember(name="johanna lindqvist")]
    )
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "duplicate_staff" in {i.code for i in warnings(issues)}


def test_model_uncertainty_is_surfaced() -> None:
    profile = _good_profile(confidence_notes="Oklart om moms ingår i priserna.")
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert any("moms" in i.message for i in warnings(issues))


def test_ingestion_warnings_are_carried_into_the_review() -> None:
    """Anything scrub_unverified deleted must reach the human."""
    doc = _doc(warnings=["Pris 1495 kr borttaget för 'Nybesök': citatet finns inte."])
    issues = validate_profile(doc, _crawl_with_name())
    assert any("borttaget" in i.message for i in warnings(issues))


def test_skipped_pdfs_warn() -> None:
    crawl = _crawl({URL: PAGE_TEXT + "\nKlinik i Stockholm."})
    crawl = type(crawl)(
        base_url=crawl.base_url,
        pages=crawl.pages,
        skipped=[("https://klinik.example/prislista.pdf", "pdf")],
        robots_txt=None,
        sitemap_urls=[],
        started_at=None,
        finished_at=None,
    )
    issues = validate_profile(_doc(), crawl)
    assert "pdfs_skipped" in {i.code for i in warnings(issues)}


# --- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def artifact_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr("ingestion.review._root", lambda: tmp_path)
    return tmp_path


def _write_artifacts(root: Path, slug: str, doc: ClinicProfileDocument) -> None:
    from ingestion import artifacts as art

    art.save_profile(root, slug, doc)
    (root / slug / "crawl.json").write_text(
        json.dumps(
            {
                "base_url": "https://klinik.example",
                "pages": [
                    {
                        "url": URL,
                        "final_url": URL,
                        "depth": 0,
                        "title": None,
                        "text": PAGE_TEXT + "\nKlinik i Stockholm.",
                        "links": [],
                        "script_srcs": [],
                        "iframe_srcs": [],
                        "word_count": 20,
                        "priority": 100,
                    }
                ],
                "skipped": [],
                "sitemap_urls": [],
            }
        ),
        encoding="utf-8",
    )


def test_check_exits_zero_on_a_clean_profile(artifact_dir: Path) -> None:
    from typer.testing import CliRunner

    from ingestion.review import app

    _write_artifacts(artifact_dir, "klinik", _doc())
    result = CliRunner().invoke(app, ["check", "klinik"])
    assert result.exit_code == 0, result.output


def test_check_exits_one_when_blocked(artifact_dir: Path) -> None:
    from typer.testing import CliRunner

    from ingestion.review import app

    _write_artifacts(artifact_dir, "klinik", _doc(_good_profile(phone=None)))
    result = CliRunner().invoke(app, ["check", "klinik"])
    assert result.exit_code == 1


def test_approve_refuses_a_blocked_profile(artifact_dir: Path) -> None:
    """The gate has to actually hold, or it protects nothing."""
    from typer.testing import CliRunner

    from ingestion.review import app

    _write_artifacts(artifact_dir, "klinik", _doc(_good_profile(services=[])))
    result = CliRunner().invoke(app, ["approve", "klinik", "--reviewer", "Fabian"])
    assert result.exit_code == 1

    from ingestion import artifacts as art

    assert art.load_profile(artifact_dir, "klinik").is_reviewed is False


def test_approve_stamps_the_reviewer(artifact_dir: Path) -> None:
    from typer.testing import CliRunner

    from ingestion import artifacts as art
    from ingestion.review import app

    _write_artifacts(artifact_dir, "klinik", _doc())
    result = CliRunner().invoke(app, ["approve", "klinik", "--reviewer", "Fabian"])
    assert result.exit_code == 0, result.output

    doc = art.load_profile(artifact_dir, "klinik")
    assert doc.is_reviewed
    assert doc.meta.reviewed_by == "Fabian"


def test_load_refuses_an_unapproved_profile(artifact_dir: Path) -> None:
    from typer.testing import CliRunner

    from ingestion.review import app

    _write_artifacts(artifact_dir, "klinik", _doc())
    result = CliRunner().invoke(app, ["load", "klinik"])
    assert result.exit_code == 1
    assert "Inte granskad" in result.output


def test_show_flags_prices_without_a_source(artifact_dir: Path) -> None:
    from typer.testing import CliRunner

    from ingestion.review import app

    profile = _good_profile(services=[ServiceItem(name="Nybesök", price_sek=895)])
    _write_artifacts(artifact_dir, "klinik", _doc(profile))
    result = CliRunner().invoke(app, ["show", "klinik"])
    assert "SAKNAR" in result.output


def test_missing_artifacts_give_an_actionable_error(artifact_dir: Path) -> None:
    from ingestion import artifacts as art

    with pytest.raises(FileNotFoundError, match="vakans-review crawl"):
        art.load_crawl(artifact_dir, "finns-inte")
    with pytest.raises(FileNotFoundError, match="vakans-review extract"):
        art.load_profile(artifact_dir, "finns-inte")


def test_reviewed_at_roundtrips_through_the_artifact(artifact_dir: Path) -> None:
    from ingestion import artifacts as art

    doc = _doc()
    doc.meta.reviewed_at = now_utc()
    doc.meta.reviewed_by = "Fabian"
    art.save_profile(artifact_dir, "klinik", doc)
    assert art.load_profile(artifact_dir, "klinik").meta.reviewed_by == "Fabian"


def test_free_care_is_not_flagged_as_a_parsing_error() -> None:
    """Found on the first real extraction: Familjeläkarna lists 0 kr for under-18s and 85+,
    which is correct Swedish primary care, not "1 195" misread as 1. Flagging it teaches the
    reviewer to skim past warnings — which is how a genuine one gets missed."""
    profile = _good_profile(
        services=[
            ServiceItem(
                name="Läkarvård - barn under 18 år",
                price_sek=0,
                price_evidence="0 kr",
                price_source_url=URL,
            )
        ]
    )
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "price_implausibly_low" not in {i.code for i in warnings(issues)}


def test_a_genuinely_suspicious_price_still_warns() -> None:
    profile = _good_profile(
        services=[
            ServiceItem(
                name="Nybesök", price_sek=1, price_evidence="1 195 kr", price_source_url=URL
            )
        ]
    )
    issues = validate_profile(_doc(profile), _crawl_with_name())
    assert "price_implausibly_low" in {i.code for i in warnings(issues)}
