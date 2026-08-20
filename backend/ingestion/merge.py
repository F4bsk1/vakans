"""Merge per-chunk extractions, then delete anything the source text does not support.

Two jobs, both deliberately free of LLM calls.

**merge_partials** unions many `PartialProfile`s into one `ClinicProfile`. A deterministic merge
is testable, costs nothing, and cannot invent a service that no chunk reported. An LLM merge
pass can do all three badly.

**scrub_unverified** is the important one. Spec §5's prompt rules ("hitta aldrig på priser") are
necessary but not sufficient — models comply most of the time, and the remainder is a dead
deal. So compliance is enforced mechanically: every price must carry a verbatim quote that is
*literally present* in the text we crawled from the URL it cites. Anything else is deleted and
reported to the reviewer.

The spec puts it plainly: "Fel pris i demo = död affär."
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence

from app.schemas.clinic_profile import (
    ClinicProfile,
    FaqItem,
    OpeningHours,
    Policies,
    Practical,
    ServiceItem,
    StaffMember,
)
from ingestion.crawl import CrawlResult

logger = logging.getLogger("vakans.ingestion.merge")

# Suffixes clinics append to service names: "Massage (60 min)", "Nybesök – 45 min".
NAME_NOISE = re.compile(r"\s*[\(\[–—-]\s*\d+\s*(min|minuter|tim|h)\b[\)\]]?\s*$", re.I)
_WS = re.compile(r"\s+")


def _norm(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip()).lower()


def normalize_service_name(name: str) -> str:
    return _norm(NAME_NOISE.sub("", name))


def normalize_person_name(name: str) -> str:
    # Titles sometimes lead the name field: "Leg. fysioterapeut Johanna Lindqvist".
    stripped = re.sub(r"^(leg\.?|dr\.?|med\.?\s*dr\.?)\s+", "", _norm(name))
    return _WS.sub(" ", stripped)


def _most_common(values: Sequence[str | None]) -> str | None:
    """Most frequent non-empty value; ties break toward the earliest (highest-priority) chunk."""
    present = [v for v in values if v and v.strip()]
    if not present:
        return None
    counts = Counter(present)
    best = max(counts.values())
    for value in present:  # chunk order == priority order
        if counts[value] == best:
            return value
    return present[0]


def merge_partials(partials: Sequence) -> ClinicProfile:
    """Union many per-chunk extractions into one profile."""
    if not partials:
        return ClinicProfile(name="Okänd klinik")

    # --- services: prefer the variant that carries a price *with* evidence
    services: dict[str, ServiceItem] = {}
    for partial in partials:
        for item in partial.services:
            if not item.name or not item.name.strip():
                continue
            key = normalize_service_name(item.name)
            existing = services.get(key)
            if existing is None or _service_rank(item) > _service_rank(existing):
                services[key] = item

    # --- staff: prefer entries that carry a title
    staff: dict[str, StaffMember] = {}
    for partial in partials:
        for person in partial.staff:
            if not person.name or not person.name.strip():
                continue
            key = normalize_person_name(person.name)
            existing = staff.get(key)
            if existing is None or (person.title and not existing.title):
                staff[key] = person

    # --- opening hours: take the most complete set rather than stitching partial ones together
    best_hours: list[OpeningHours] = []
    for partial in partials:
        if len(partial.opening_hours) > len(best_hours):
            best_hours = partial.opening_hours

    # --- faq: dedupe on the question
    faq: dict[str, FaqItem] = {}
    for partial in partials:
        for item in partial.faq:
            key = _norm(item.q)
            if key and key not in faq:
                faq[key] = item

    # --- policies and practical: field-by-field first-non-empty, in chunk priority order
    policies = Policies(
        cancellation=_first(partials, lambda p: p.policies and p.policies.cancellation),
        late_fee=_first(partials, lambda p: p.policies and p.policies.late_fee),
        insurance_accepted=_union_lists(
            [p.policies.insurance_accepted for p in partials if p.policies]
        ),
        referral_needed=_first(
            partials,
            lambda p: p.policies.referral_needed if p.policies is not None else None,
            allow_false=True,
        ),
    )
    practical = Practical(
        parking=_first(partials, lambda p: p.practical and p.practical.parking),
        transit=_first(partials, lambda p: p.practical and p.practical.transit),
        accessibility=_first(partials, lambda p: p.practical and p.practical.accessibility),
    )

    notes = [p.confidence_notes.strip() for p in partials if p.confidence_notes.strip()]

    return ClinicProfile(
        name=_most_common([p.name for p in partials]) or "Okänd klinik",
        city=_most_common([p.city for p in partials]),
        address=_most_common([p.address for p in partials]),
        phone=_most_common([p.phone for p in partials]),
        email=_most_common([p.email for p in partials]),
        opening_hours=best_hours,
        staff=list(staff.values()),
        services=list(services.values()),
        policies=policies,
        practical=practical,
        faq=list(faq.values()),
        confidence_notes=" ".join(dict.fromkeys(notes)),
    )


def _service_rank(item: ServiceItem) -> int:
    """Priced-with-evidence beats priced beats named."""
    if item.price_sek is not None and item.price_evidence:
        return 3
    if item.price_sek is not None:
        return 2
    if item.duration_min:
        return 1
    return 0


def _first(partials: Sequence, getter, *, allow_false: bool = False):
    for partial in partials:
        try:
            value = getter(partial)
        except AttributeError:
            continue
        if value is not None and (allow_false or value != ""):
            return value
    return None


def _union_lists(lists: Sequence[Sequence[str]]) -> list[str]:
    seen: dict[str, str] = {}
    for items in lists:
        for item in items:
            key = _norm(item)
            if key and key not in seen:
                seen[key] = item
    return list(seen.values())


# --- the anti-hallucination pass ---------------------------------------------------------------


def quote_is_present(quote: str | None, text: str) -> bool:
    """Is this quote literally in the source text, ignoring whitespace and case?

    Whitespace-insensitive because the model reflows what it copies; otherwise exact. Numbers
    are compared as written, so "895" cannot pass on a page that only says "859".
    """
    if not quote or not quote.strip():
        return False
    needle = _norm(quote)
    haystack = _norm(text)
    if needle in haystack:
        return True
    # Swedish prices are commonly written "1 195 kr" with a space or non-breaking space.
    return needle.replace(" ", " ").replace(" ", "") in haystack.replace(" ", "")


def scrub_unverified(profile: ClinicProfile, crawl: CrawlResult) -> tuple[ClinicProfile, list[str]]:
    """Delete every fact the crawled text does not support, and report what was removed.

    This is the mechanism behind "never invent prices" — the prompt asks, this enforces.
    """
    corpus = crawl.corpus
    all_text = "\n".join(corpus.values())
    crawled_urls = set(corpus)
    warnings: list[str] = []

    # --- prices
    for service in profile.services:
        if service.price_sek is None:
            continue

        if not service.price_evidence:
            warnings.append(f"Pris borttaget för '{service.name}': inget källcitat angavs.")
            service.price_sek = None
            service.price_source_url = None
            continue

        cited = service.price_source_url
        source_text = corpus.get(cited) if cited else None

        if source_text is not None and quote_is_present(service.price_evidence, source_text):
            continue
        # The model sometimes cites a sibling URL but quotes real text; accept the quote if it
        # appears anywhere we crawled, and say so.
        if quote_is_present(service.price_evidence, all_text):
            if cited not in crawled_urls:
                warnings.append(
                    f"Pris för '{service.name}' behölls men källadressen stämde inte: {cited}"
                )
            continue

        warnings.append(
            f"Pris {service.price_sek} kr borttaget för '{service.name}': citatet "
            f'"{service.price_evidence[:60]}" finns inte i den hämtade texten.'
        )
        service.price_sek = None
        service.price_source_url = None
        service.price_evidence = None

    # --- faq
    kept_faq: list[FaqItem] = []
    for item in profile.faq:
        if item.source_url in crawled_urls:
            kept_faq.append(item)
        else:
            warnings.append(
                f"FAQ borttagen ('{item.q[:40]}'): källadressen {item.source_url} hämtades aldrig."
            )
    profile.faq = kept_faq

    # --- staff: a fabricated practitioner name is as damaging as a fabricated price, because
    # the agent will offer appointments with a person who does not work there.
    kept_staff: list[StaffMember] = []
    for person in profile.staff:
        if quote_is_present(person.name, all_text):
            kept_staff.append(person)
        else:
            warnings.append(f"Behandlare borttagen: '{person.name}' finns inte i texten.")
    profile.staff = kept_staff

    # --- wrong-site guard
    if profile.name and not quote_is_present(profile.name, all_text):
        warnings.append(
            f"Kliniknamnet '{profile.name}' finns inte i den hämtade texten — kontrollera att "
            "rätt webbplats crawlades."
        )

    if pdfs := crawl.skipped_pdfs():
        warnings.append(
            f"{len(pdfs)} PDF-filer hoppades över och kan innehålla prislistan: "
            + ", ".join(pdfs[:3])
        )

    return profile, warnings
