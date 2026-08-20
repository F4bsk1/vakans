"""Which booking system does this clinic already run? — spec §5's `booking_system_hint`.

**Deliberately not an LLM job.** This is a pattern match over hrefs, `<script src>` and
`<iframe src>`, so a deterministic detector is more accurate, free, testable and available with
no credentials. The extracted value from the model is overridden by whatever this returns.

The spec notes the field "styr er adapterpitch" — it decides whether you walk into the meeting
saying "we integrate with Kaddio" or "we integrate with Muntra". Being confidently wrong about
it is worse than saying nothing, which is why the evidence list is surfaced in `review.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.schemas.clinic_profile import BookingSystemHint
from ingestion.crawl import Page

# host substring -> hint. Ordered most specific first.
VENDOR_HOSTS: tuple[tuple[str, BookingSystemHint], ...] = (
    ("kaddio.com", BookingSystemHint.KADDIO),
    ("kaddio.se", BookingSystemHint.KADDIO),
    ("muntra.com", BookingSystemHint.MUNTRA),
    ("muntra.se", BookingSystemHint.MUNTRA),
    ("bokadirekt.se", BookingSystemHint.BOKADIREKT),
    ("bokamera.se", BookingSystemHint.BOKAMERA),
    ("bokamera.com", BookingSystemHint.BOKAMERA),
    ("timecenter.se", BookingSystemHint.TIMECENTER),
    ("timecenter.com", BookingSystemHint.TIMECENTER),
    ("cliniko.com", BookingSystemHint.CLINIKO),
    ("easypractice.net", BookingSystemHint.EASYPRACTICE),
    ("easypractice.se", BookingSystemHint.EASYPRACTICE),
    ("boka.se", BookingSystemHint.WEBBOKNING),
    ("webbokning", BookingSystemHint.WEBBOKNING),
)

# Text that implies online booking exists without saying which system.
BOOKING_TEXT = re.compile(r"boka (tid|online|direkt)|boka nu|online-?bokning|webbokning", re.I)
# A booking subdomain of the clinic's own domain — self-hosted or an unrecognised vendor.
BOOKING_SUBDOMAIN = re.compile(r"^(boka|booking|bokning|tidbok|tidsbokning)\.", re.I)

CONFIDENCE_EMBED = 0.95  # script/iframe: the system is actually running on the page
CONFIDENCE_LINK = 0.70  # a link out to the vendor
CONFIDENCE_TEXT = 0.40  # only prose


@dataclass(frozen=True)
class BookingDetection:
    hint: BookingSystemHint | None
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def is_actionable(self) -> bool:
        """Confident enough to name the vendor in a sales conversation."""
        return (
            self.hint not in (None, BookingSystemHint.NONE) and self.confidence >= CONFIDENCE_LINK
        )


def _match_vendor(url: str) -> BookingSystemHint | None:
    host = (urlparse(url).netloc or url).lower()
    for needle, hint in VENDOR_HOSTS:
        if needle in host:
            return hint
    # Some sites reference the vendor in a path rather than a host.
    lowered = url.lower()
    for needle, hint in VENDOR_HOSTS:
        if "." in needle and needle in lowered:
            return hint
    return None


def detect_booking_system(pages: Sequence[Page], *, base_url: str = "") -> BookingDetection:
    embeds: dict[BookingSystemHint, list[str]] = {}
    links: dict[BookingSystemHint, list[str]] = {}
    custom_evidence: list[str] = []
    text_evidence: list[str] = []

    for page in pages:
        # Embedded widgets are the strongest signal: the system runs on the page.
        for url in (*page.script_srcs, *page.iframe_srcs):
            hint = _match_vendor(url)
            if hint:
                embeds.setdefault(hint, []).append(url)
            elif BOOKING_SUBDOMAIN.match(urlparse(url).netloc or ""):
                custom_evidence.append(url)

        for url in page.links:
            hint = _match_vendor(url)
            if hint:
                links.setdefault(hint, []).append(url)
                continue
            host = urlparse(url).netloc
            if BOOKING_SUBDOMAIN.match(host) and (not base_url or _same_root(host, base_url)):
                custom_evidence.append(url)

        match = BOOKING_TEXT.search(page.text)
        if match:
            text_evidence.append(f"{page.final_url}: …{match.group(0)}…")

    if embeds:
        hint = max(embeds, key=lambda h: len(embeds[h]))
        return BookingDetection(hint, _dedupe(embeds[hint])[:5], CONFIDENCE_EMBED)

    if links:
        hint = max(links, key=lambda h: len(links[h]))
        return BookingDetection(hint, _dedupe(links[hint])[:5], CONFIDENCE_LINK)

    if custom_evidence:
        # They book online, but not through a system we recognise. Worth knowing: it usually
        # means a bespoke integration conversation rather than an off-the-shelf adapter.
        return BookingDetection(
            BookingSystemHint.CUSTOM, _dedupe(custom_evidence)[:5], CONFIDENCE_LINK
        )

    if text_evidence:
        return BookingDetection(
            BookingSystemHint.CUSTOM, _dedupe(text_evidence)[:5], CONFIDENCE_TEXT
        )

    # No booking affordance at all — phone-only, which is the strongest possible pitch for us.
    return BookingDetection(BookingSystemHint.NONE, [], CONFIDENCE_LINK)


def _same_root(host: str, base_url: str) -> bool:
    base_host = urlparse(base_url).netloc.lower().removeprefix("www.")
    root = ".".join(base_host.split(".")[-2:])
    return host.lower().endswith(root)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
