"""LLM extraction of a `clinic_profile` from crawled pages — spec §5.

Two passes, plus a deterministic third:

1. Per-chunk extraction into `PartialProfile` (concurrent).
2. A **deterministic Python merge** (`merge.py`), not a second LLM call — testable, free, and
   incapable of hallucinating.
3. `scrub_unverified`, which deletes any price whose evidence quote is not literally present on
   the page it cites.

Chunks are packed in descending page priority, so the price list lands in chunk 0 where the
model pays the most attention.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.clinic_profile import (
    ClinicProfile,
    ClinicProfileDocument,
    FaqItem,
    OpeningHours,
    Policies,
    Practical,
    ProfileMeta,
    ServiceItem,
    StaffMember,
)
from ingestion.crawl import CrawlResult
from ingestion.detect import BookingDetection
from ingestion.llm import Chunk, ProfileExtractor

logger = logging.getLogger("vakans.ingestion.extract")

MAX_CHUNK_CHARS = 12_000
CONCURRENCY = 3


class PartialProfile(BaseModel):
    """One chunk's worth of findings. Everything optional — a chunk sees only part of a site.

    `extra="forbid"` on purpose: under structured extraction an invented key should be a loud
    validation error that triggers a retry, not data quietly dropped on the floor.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    city: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    opening_hours: list[OpeningHours] = Field(default_factory=list)
    staff: list[StaffMember] = Field(default_factory=list)
    services: list[ServiceItem] = Field(default_factory=list)
    policies: Policies | None = None
    practical: Practical | None = None
    faq: list[FaqItem] = Field(default_factory=list)
    confidence_notes: str = ""


EXTRACTION_PROMPT = """\
You extract structured facts about a Swedish healthcare clinic from text scraped off its own \
website. The text is Swedish. Your output is used to configure a phone agent that speaks to \
real patients, so a wrong fact is worse than a missing one.

ABSOLUTE RULES

1. Extract only what is explicitly present in the supplied text. Never infer, never complete \
from general knowledge, never carry a fact over from a similar clinic. If you are unsure, \
return null and say why in confidence_notes.

2. PRICES. Never guess a price. Every service with a price_sek must also carry:
   - price_evidence: a VERBATIM quote from the supplied text containing that number, copied \
character for character.
   - price_source_url: the ### SOURCE_URL that the quote appeared under.
   A price without a verbatim quote will be discarded automatically, so inventing one gains \
you nothing. If a price is written as a range or "från 800 kr", take the lowest number and \
quote the whole phrase.

3. DURATIONS. Only fill duration_min when the text states a length. Do not infer it from the \
service name or a price.

4. FAQ. Every faq item needs a source_url taken from a ### SOURCE_URL header in this chunk. \
Write the "a" field as SPOKEN SWEDISH: the phone agent reads it aloud. One to three sentences, \
no bullet points, no markdown, no links. Turn "Vi har öppet mån-fre 08-17" into "Vi har öppet \
vardagar mellan klockan åtta och sjutton."

5. Keep everything in Swedish. Do not translate. Keep staff names and titles exactly as \
written, including "Leg." prefixes.

6. Do not populate booking_system_hint. It is determined separately and deterministically.

7. opening_hours uses lowercase three-letter days: mon, tue, wed, thu, fri, sat, sun. Set \
closed=true rather than inventing 00:00 times.

8. confidence_notes is for a human reviewer, written in Swedish. Note anything ambiguous: \
conflicting prices on different pages, unclear whether a price includes VAT, staff who may \
have left, opening hours that looked seasonal.

Extract from the following pages.\
"""


def build_chunks(pages: Sequence, *, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Pack pages into chunks, highest-priority first.

    Ordering matters: sorting by priority means the price page is in chunk 0, which is where
    the model is most reliable. A page is never split across chunks unless it alone exceeds the
    limit.
    """
    ordered = sorted(pages, key=lambda p: (-getattr(p, "priority", 0), p.url))
    chunks: list[Chunk] = []
    current: list[str] = []
    current_urls: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_urls, current_len
        if current:
            chunks.append(
                Chunk(index=len(chunks), urls=list(current_urls), text="\n\n".join(current))
            )
            current, current_urls, current_len = [], [], 0

    for page in ordered:
        header = f"### SOURCE_URL: {page.final_url}\n### TITLE: {page.title or ''}\n"
        body = page.text

        if len(header) + len(body) > max_chars:
            flush()
            # Split an oversized page, repeating the header so every part keeps its source.
            budget = max_chars - len(header)
            for start in range(0, len(body), budget):
                chunks.append(
                    Chunk(
                        index=len(chunks),
                        urls=[page.final_url],
                        text=header + body[start : start + budget],
                    )
                )
            continue

        if current_len + len(header) + len(body) > max_chars:
            flush()
        current.append(header + body)
        current_urls.append(page.final_url)
        current_len += len(header) + len(body)

    flush()
    return chunks


async def extract_profile(
    crawl: CrawlResult,
    extractor: ProfileExtractor,
    *,
    detection: BookingDetection,
    concurrency: int = CONCURRENCY,
) -> ClinicProfileDocument:
    """Crawled pages -> a reviewed-ready profile document."""
    from ingestion.merge import merge_partials, scrub_unverified

    chunks = build_chunks(crawl.pages)
    logger.info("extracting from %d chunks using %s", len(chunks), extractor.name)

    sem = asyncio.Semaphore(concurrency)

    async def run(chunk: Chunk) -> PartialProfile | None:
        async with sem:
            try:
                return await extractor.extract(chunk)  # type: ignore[return-value]
            except Exception as exc:
                # One bad chunk must not lose the other nine.
                logger.warning("chunk %d failed: %s", chunk.index, exc)
                return None

    results = await asyncio.gather(*(run(c) for c in chunks))
    partials = [p for p in results if p is not None]

    merged = merge_partials(partials)
    # Deterministic detection always wins over whatever the model guessed.
    merged.booking_system_hint = detection.hint

    scrubbed, warnings = scrub_unverified(merged, crawl)
    if len(partials) < len(chunks):
        warnings.append(f"{len(chunks) - len(partials)} av {len(chunks)} textblock misslyckades")

    return ClinicProfileDocument(
        profile=scrubbed,
        meta=ProfileMeta(
            source="ingestion",
            base_url=crawl.base_url,
            crawled_at=crawl.started_at,
            pages_used=[p.final_url for p in crawl.pages],
            extractor=extractor.name,
            warnings=warnings,
        ),
    )


def empty_profile(name: str = "Okänd klinik") -> ClinicProfile:
    return ClinicProfile(name=name)


__all__ = [
    "EXTRACTION_PROMPT",
    "MAX_CHUNK_CHARS",
    "PartialProfile",
    "build_chunks",
    "empty_profile",
    "extract_profile",
]
