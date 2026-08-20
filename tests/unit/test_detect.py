"""Booking-system detection. Spec §5: this field "styr er adapterpitch", so it must be right."""

from __future__ import annotations

import pytest

from app.schemas.clinic_profile import BookingSystemHint
from ingestion.crawl import Page
from ingestion.detect import detect_booking_system

BASE = "https://klinik.example"


def _page(
    *,
    text: str = "Välkommen till kliniken.",
    links: tuple[str, ...] = (),
    scripts: tuple[str, ...] = (),
    iframes: tuple[str, ...] = (),
) -> Page:
    return Page(
        url=f"{BASE}/",
        final_url=f"{BASE}/",
        depth=0,
        title="Start",
        text=text,
        links=list(links),
        script_srcs=list(scripts),
        iframe_srcs=list(iframes),
        word_count=len(text.split()),
    )


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("https://app.kaddio.com/widget.js", BookingSystemHint.KADDIO),
        ("https://www.muntra.com/embed.js", BookingSystemHint.MUNTRA),
        ("https://www.bokadirekt.se/embed/1234", BookingSystemHint.BOKADIREKT),
        ("https://bokamera.se/widget", BookingSystemHint.BOKAMERA),
        ("https://timecenter.se/booking", BookingSystemHint.TIMECENTER),
        ("https://klinik.cliniko.com/bookings", BookingSystemHint.CLINIKO),
        ("https://easypractice.net/book", BookingSystemHint.EASYPRACTICE),
    ],
)
def test_each_vendor_is_recognised_from_an_embed(src: str, expected: BookingSystemHint) -> None:
    result = detect_booking_system([_page(scripts=(src,))], base_url=BASE)
    assert result.hint is expected
    assert result.confidence == 0.95  # an embed means the system runs on the page
    assert result.evidence == [src]
    assert result.is_actionable


def test_iframe_embeds_count_as_strongly_as_scripts() -> None:
    result = detect_booking_system(
        [_page(iframes=("https://www.bokadirekt.se/embed/1234",))], base_url=BASE
    )
    assert result.hint is BookingSystemHint.BOKADIREKT
    assert result.confidence == 0.95


def test_a_plain_link_is_weaker_than_an_embed() -> None:
    result = detect_booking_system(
        [_page(links=("https://www.bokadirekt.se/places/klinik",))], base_url=BASE
    )
    assert result.hint is BookingSystemHint.BOKADIREKT
    assert result.confidence == 0.70
    assert result.is_actionable


def test_an_embed_wins_over_a_link_to_a_different_vendor() -> None:
    """Clinics link to Bokadirekt for marketing while actually running Kaddio."""
    result = detect_booking_system(
        [
            _page(
                links=("https://www.bokadirekt.se/places/klinik",),
                scripts=("https://app.kaddio.com/widget.js",),
            )
        ],
        base_url=BASE,
    )
    assert result.hint is BookingSystemHint.KADDIO


def test_own_booking_subdomain_is_custom() -> None:
    result = detect_booking_system([_page(links=("https://boka.klinik.example/",))], base_url=BASE)
    assert result.hint is BookingSystemHint.CUSTOM
    assert result.evidence


def test_booking_prose_alone_is_low_confidence_custom() -> None:
    result = detect_booking_system([_page(text="Du kan boka tid online hos oss.")], base_url=BASE)
    assert result.hint is BookingSystemHint.CUSTOM
    assert result.confidence == 0.40
    # Not confident enough to name a vendor in a sales meeting.
    assert not result.is_actionable


def test_no_booking_affordance_at_all_is_none() -> None:
    """Phone-only is the strongest possible pitch for us, so it must be distinguishable from
    "we could not tell"."""
    result = detect_booking_system([_page(text="Ring oss på 08-123 45 67.")], base_url=BASE)
    assert result.hint is BookingSystemHint.NONE
    assert result.evidence == []


def test_the_most_referenced_vendor_wins() -> None:
    pages = [
        _page(scripts=("https://app.kaddio.com/a.js",)),
        _page(scripts=("https://app.kaddio.com/b.js",)),
        _page(scripts=("https://www.muntra.com/x.js",)),
    ]
    assert detect_booking_system(pages, base_url=BASE).hint is BookingSystemHint.KADDIO


def test_evidence_is_deduplicated_and_capped() -> None:
    src = "https://app.kaddio.com/widget.js"
    pages = [_page(scripts=(src,)) for _ in range(10)]
    result = detect_booking_system(pages, base_url=BASE)
    assert result.evidence == [src]


def test_empty_crawl_does_not_crash() -> None:
    assert detect_booking_system([], base_url=BASE).hint is BookingSystemHint.NONE
