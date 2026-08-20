"""Chunking, the extraction prompt's invariants, and the offline extraction path."""

from __future__ import annotations

from app.schemas.clinic_profile import BookingSystemHint
from ingestion.crawl import Page
from ingestion.detect import BookingDetection
from ingestion.extract import (
    EXTRACTION_PROMPT,
    PartialProfile,
    build_chunks,
    extract_profile,
)
from ingestion.llm import Chunk, FixtureExtractor
from tests.unit.test_merge import _crawl


def _page(url: str, text: str, priority: int = 10, title: str = "T") -> Page:
    return Page(
        url=url,
        final_url=url,
        depth=0,
        title=title,
        text=text,
        word_count=len(text.split()),
        priority=priority,
    )


def test_chunks_are_ordered_by_page_priority() -> None:
    """The price page must land in chunk 0, where the model is most reliable."""
    pages = [
        _page("https://k.example/blogg", "blogginnehåll", priority=10),
        _page("https://k.example/priser", "Nybesök 895 kr", priority=100),
        _page("https://k.example/kontakt", "08-123 45 67", priority=70),
    ]
    chunks = build_chunks(pages, max_chars=100_000)
    assert chunks[0].urls[0] == "https://k.example/priser"
    assert chunks[0].urls[-1] == "https://k.example/blogg"


def test_every_chunk_carries_source_url_headers() -> None:
    """The prompt requires FAQ items to cite a ### SOURCE_URL from their own chunk."""
    chunks = build_chunks([_page("https://k.example/priser", "Nybesök 895 kr")])
    assert "### SOURCE_URL: https://k.example/priser" in chunks[0].text
    assert "### TITLE:" in chunks[0].text


def test_pages_are_packed_up_to_the_limit() -> None:
    pages = [_page(f"https://k.example/{i}", "x" * 400) for i in range(10)]
    chunks = build_chunks(pages, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c.text) <= 1200 for c in chunks)  # header overhead allowed


def test_a_page_is_not_split_unless_it_alone_exceeds_the_limit() -> None:
    pages = [_page("https://k.example/a", "x" * 300), _page("https://k.example/b", "y" * 300)]
    chunks = build_chunks(pages, max_chars=1000)
    assert len(chunks) == 1


def test_an_oversized_page_is_split_with_the_header_repeated() -> None:
    """A split part with no source URL would produce FAQ items the scrubber then deletes."""
    chunks = build_chunks([_page("https://k.example/lang", "x" * 5000)], max_chars=1000)
    assert len(chunks) > 1
    assert all("### SOURCE_URL: https://k.example/lang" in c.text for c in chunks)


def test_no_pages_yields_no_chunks() -> None:
    assert build_chunks([]) == []


def test_chunk_digest_is_stable_and_content_addressed() -> None:
    a = Chunk(index=0, urls=["u"], text="samma text")
    b = Chunk(index=7, urls=["annan"], text="samma text")
    assert a.digest == b.digest  # fixtures key on content, not position


# --- the prompt ---------------------------------------------------------------------------------


def test_prompt_states_the_rules_the_scrubber_enforces() -> None:
    """If the prompt and merge.scrub_unverified disagree, every extraction loses its prices."""
    assert "price_evidence" in EXTRACTION_PROMPT
    assert "VERBATIM" in EXTRACTION_PROMPT
    assert "price_source_url" in EXTRACTION_PROMPT
    assert "source_url" in EXTRACTION_PROMPT


def test_prompt_asks_for_spoken_swedish_faq_answers() -> None:
    """The agent reads these aloud; markdown bullets would be spoken literally."""
    assert "SPOKEN SWEDISH" in EXTRACTION_PROMPT
    assert "no markdown" in EXTRACTION_PROMPT


def test_prompt_defers_booking_system_hint_to_the_deterministic_detector() -> None:
    assert "Do not populate booking_system_hint" in EXTRACTION_PROMPT


# --- end to end, offline ---------------------------------------------------------------------


async def test_extract_profile_runs_with_no_credentials() -> None:
    crawl = _crawl()
    chunks = build_chunks(crawl.pages)
    recorded = {
        chunks[0].digest: {
            "name": "Klinik",
            "services": [
                {
                    "name": "Nybesök fysioterapi",
                    "price_sek": 895,
                    "price_evidence": "Nybesök fysioterapi 895 kr",
                    "price_source_url": "https://klinik.example/priser",
                }
            ],
            "staff": [{"name": "Johanna Lindqvist", "title": "Leg. fysioterapeut"}],
        }
    }
    extractor = FixtureExtractor(recorded, PartialProfile)
    detection = BookingDetection(BookingSystemHint.KADDIO, ["https://app.kaddio.com/w.js"], 0.95)

    doc = await extract_profile(crawl, extractor, detection=detection)

    assert doc.profile.services[0].price_sek == 895
    assert doc.profile.staff[0].name == "Johanna Lindqvist"
    assert doc.meta.source == "ingestion"
    assert doc.meta.extractor == "fixture"
    assert doc.is_reviewed is False  # ingestion output is never pre-approved


async def test_deterministic_detection_overrides_the_model() -> None:
    """Spec §5 says this field drives the adapter pitch, so the pattern match wins."""
    crawl = _crawl()
    extractor = FixtureExtractor({}, PartialProfile)
    detection = BookingDetection(BookingSystemHint.MUNTRA, ["https://muntra.com/x.js"], 0.95)

    doc = await extract_profile(crawl, extractor, detection=detection)
    assert doc.profile.booking_system_hint is BookingSystemHint.MUNTRA


async def test_a_failing_chunk_does_not_lose_the_others() -> None:
    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        async def extract(self, chunk: Chunk) -> PartialProfile:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("model timed out")
            return PartialProfile(name="Klinik")

    crawl = _crawl(
        {
            "https://klinik.example/priser": "Nybesök fysioterapi 895 kr " * 400,
            "https://klinik.example/kontakt": "Klinik. Telefon 08-123 45 67. " * 400,
        }
    )
    doc = await extract_profile(
        crawl, Flaky(), detection=BookingDetection(BookingSystemHint.NONE, [], 0.7)
    )
    assert any("misslyckades" in w for w in doc.meta.warnings)


async def test_extraction_records_which_pages_were_used() -> None:
    crawl = _crawl()
    doc = await extract_profile(
        crawl,
        FixtureExtractor({}, PartialProfile),
        detection=BookingDetection(BookingSystemHint.NONE, [], 0.7),
    )
    assert doc.meta.pages_used == [p.final_url for p in crawl.pages]
    assert doc.meta.base_url == "https://klinik.example"
