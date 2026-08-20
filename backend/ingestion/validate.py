"""Profile quality rules — the machine half of spec §5's "obligatorisk QA-grind".

Pure and I/O-free, so the API can reuse it and every rule is trivially testable.

Blocking issues stop a profile from being approved at all. Warnings are things a human should
look at but that need not stop a demo. The split matters: if everything blocks, people learn to
pass `--force`, and then the gate protects nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.clinic_profile import BookingSystemHint, ClinicProfileDocument
from ingestion.crawl import CrawlResult
from ingestion.merge import quote_is_present

# A Swedish clinic visit outside this range is almost certainly a parsing error: "1 195 kr"
# read as 1, or a package deal read as a single treatment.
MIN_PLAUSIBLE_PRICE = 50
MAX_PLAUSIBLE_PRICE = 5000
MAX_PLAUSIBLE_SERVICES = 25


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    field: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is Severity.BLOCK


def validate_profile(doc: ClinicProfileDocument, crawl: CrawlResult | None = None) -> list[Issue]:
    profile = doc.profile
    issues: list[Issue] = []

    def block(code: str, message: str, field: str | None = None) -> None:
        issues.append(Issue(Severity.BLOCK, code, message, field))

    def warn(code: str, message: str, field: str | None = None) -> None:
        issues.append(Issue(Severity.WARN, code, message, field))

    # --- blocking: without these the agent cannot do its job at all
    if not profile.name or not profile.name.strip():
        block("no_name", "Kliniken saknar namn.", "name")
    if not profile.phone:
        block("no_phone", "Inget telefonnummer — agenten kan inte hänvisa vidare.", "phone")
    if not profile.services:
        block("no_services", "Inga tjänster hittades — det går inte att boka något.", "services")
    if not profile.staff:
        block("no_staff", "Inga behandlare hittades — kalendern får inga kolumner.", "staff")
    if not profile.opening_hours:
        block("no_hours", "Inga öppettider hittades.", "opening_hours")

    for service in profile.services:
        if service.price_sek is None:
            continue
        if not (service.price_evidence and service.price_source_url):
            block(
                "price_without_evidence",
                f"'{service.name}' har pris {service.price_sek} kr utan källcitat.",
                "services",
            )

    for item in profile.faq:
        if not item.source_url:
            block("faq_without_source", f"FAQ '{item.q[:40]}' saknar källa.", "faq")

    # Wrong-site guard: we may have crawled a directory listing or the wrong domain entirely.
    if (
        crawl is not None
        and profile.name
        and not quote_is_present(profile.name, "\n".join(crawl.corpus.values()))
    ):
        block(
            "name_not_in_source",
            f"Namnet '{profile.name}' finns inte i den hämtade texten — fel webbplats?",
            "name",
        )

    # --- warnings: look at these, but they need not stop a demo
    if profile.booking_system_hint in (None, BookingSystemHint.CUSTOM):
        warn(
            "booking_system_unknown",
            "Bokningssystemet kunde inte identifieras — påverkar adapterpitchen.",
            "booking_system_hint",
        )
    if not profile.policies.cancellation:
        warn(
            "no_cancellation_policy",
            "Ingen avbokningsregel — det är en av de vanligaste frågorna till receptionen.",
            "policies",
        )
    if len(profile.services) > MAX_PLAUSIBLE_SERVICES:
        warn(
            "too_many_services",
            f"{len(profile.services)} tjänster extraherades — troligen överextraktion.",
            "services",
        )

    for service in profile.services:
        if service.price_sek is None:
            continue
        if service.price_sek < MIN_PLAUSIBLE_PRICE:
            warn(
                "price_implausibly_low",
                f"'{service.name}' {service.price_sek} kr — kan vara feltolkat "
                '(t.ex. "1 195" läst som 1).',
                "services",
            )
        elif service.price_sek > MAX_PLAUSIBLE_PRICE:
            warn(
                "price_implausibly_high",
                f"'{service.name}' {service.price_sek} kr — paketpris eller feltolkning?",
                "services",
            )

    names = [s.name.strip().lower() for s in profile.staff if s.name]
    for duplicate in {n for n in names if names.count(n) > 1}:
        warn("duplicate_staff", f"Behandlaren '{duplicate}' förekommer flera gånger.", "staff")

    if not profile.faq:
        warn("no_faq", "Inga FAQ-poster — agenten får inget att svara med på praktiska frågor.")

    if profile.confidence_notes.strip():
        warn("model_uncertain", f"Modellens egna osäkerheter: {profile.confidence_notes}")

    for warning in doc.meta.warnings:
        warn("ingestion_warning", warning)

    if crawl is not None and (pdfs := crawl.skipped_pdfs()):
        warn(
            "pdfs_skipped",
            f"{len(pdfs)} PDF hoppades över och kan innehålla prislistan — kontrollera manuellt.",
        )

    return issues


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.blocking]


def warnings(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if not i.blocking]


def is_approvable(issues: list[Issue]) -> bool:
    return not blocking(issues)
