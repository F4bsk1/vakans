"""Crawl a clinic site — spec §5.

sitemap.xml, falling back to a breadth-first crawl from the homepage. Max depth 2, max 40
pages, same domain only, robots.txt respected, never behind a login.

**The highest-leverage detail here is `url_priority`.** Forty pages of a clinic's blog produces
a useless profile. The frontier is a priority queue keyed on Swedish path keywords, so the
budget is spent on the pages that actually carry prices, staff names and opening hours. Getting
this wrong is not a crash — it is a demo where the prices are missing and nobody knows why.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import heapq
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from lxml import html as lxml_html

from app.timeutils import now_utc
from ingestion.fetchers import Fetcher, FetchResult

logger = logging.getLogger("vakans.ingestion.crawl")

# Swedish path keywords, highest value first. A clinic's price list is worth more than its
# entire blog archive, and the 40-page budget has to reflect that.
PRIORITY_RULES: tuple[tuple[int, re.Pattern[str]], ...] = (
    (100, re.compile(r"pris|prislista|kostnad|avgift|taxa", re.I)),
    (90, re.compile(r"behandling|tjanst|tjänst|vad-vi-gor|vad-vi-gör|utbud|terapi", re.I)),
    (80, re.compile(r"personal|medarbetare|team|om-oss|om_oss|behandlare|terapeut|klinik", re.I)),
    (70, re.compile(r"kontakt|hitta-hit|hitta_hit|oppettider|öppettider|boka|bokning", re.I)),
    (
        60,
        re.compile(
            r"faq|vanliga-fragor|vanliga-frågor|villkor|avbokning|integritet|forsakring", re.I
        ),
    ),
)
HOMEPAGE_PRIORITY = 95
DEFAULT_PRIORITY = 10

# A blog archive will happily eat the whole budget.
EXCLUDE_PATTERN = re.compile(
    r"/(blogg|blog|nyhet|nyheter|artikel|artiklar|kategori|category|tag|taggar|author|"
    r"wp-admin|wp-json|wp-content|feed|rss|sok|search|cart|kassa|checkout|min-sida|logga-in|login)",
    re.I,
)
NON_HTML_SUFFIXES = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|_ga)", re.I)

SITEMAP_CANDIDATES = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/wp-sitemap.xml",  # WordPress dominates this market segment
    "/sitemap-index.xml",
)


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = 40
    max_depth: int = 2
    same_domain_only: bool = True
    include_subdomains: bool = True
    respect_robots: bool = True
    user_agent: str = "VakansBot/0.1"
    concurrency: int = 3
    min_words: int = 20  # below this a "page" is a nav shell, not content


@dataclass(frozen=True)
class Page:
    url: str
    final_url: str
    depth: int
    title: str | None
    text: str
    links: list[str] = field(default_factory=list)
    script_srcs: list[str] = field(default_factory=list)
    iframe_srcs: list[str] = field(default_factory=list)
    word_count: int = 0
    priority: int = DEFAULT_PRIORITY
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class CrawlResult:
    base_url: str
    pages: list[Page]
    skipped: list[tuple[str, str]]  # (url, reason)
    robots_txt: str | None
    sitemap_urls: list[str]
    started_at: datetime
    finished_at: datetime

    @property
    def corpus(self) -> dict[str, str]:
        """url -> extracted text. `merge.scrub_unverified` checks price quotes against this."""
        return {p.final_url: p.text for p in self.pages}

    def skipped_pdfs(self) -> list[str]:
        return [u for u, reason in self.skipped if reason == "pdf"]


# --- URL handling ------------------------------------------------------------------------------


def normalize_url(url: str, base: str) -> str | None:
    """Absolutise, strip fragments and tracking params, drop anything not worth fetching."""
    if not url:
        return None
    url = url.strip()
    if url.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None

    absolute = urljoin(base, url)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)

    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path.lower().endswith(NON_HTML_SUFFIXES):
        return None

    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and not TRACKING_PARAMS.match(part.split("=", 1)[0])
    )
    # Trailing slash is not a distinct page. Many sites serve /priser and /priser/ identically,
    # and without this the budget is spent crawling the same content twice.
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", query, ""))


def same_site(url: str, base: str, *, include_subdomains: bool = True) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(base).netloc.lower()
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    if a == b:
        return True
    return include_subdomains and (a.endswith(f".{b}") or b.endswith(f".{a}"))


def url_priority(url: str, base: str) -> int:
    """Score a URL for the frontier. Higher is fetched first."""
    parsed = urlparse(url)
    path = parsed.path or "/"

    if EXCLUDE_PATTERN.search(path):
        return 0
    if path == "/" and same_site(url, base):
        return HOMEPAGE_PRIORITY
    for score, pattern in PRIORITY_RULES:
        if pattern.search(path):
            return score
    return DEFAULT_PRIORITY


def is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


# --- HTML -------------------------------------------------------------------------------------


def extract_links(html: str, base: str) -> tuple[list[str], list[str], list[str]]:
    """(hrefs, script srcs, iframe srcs). The latter two feed booking-system detection."""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return [], [], []

    hrefs = [urljoin(base, h) for h in doc.xpath("//a/@href") if h]
    scripts = [urljoin(base, s) for s in doc.xpath("//script/@src") if s]
    iframes = [urljoin(base, s) for s in doc.xpath("//iframe/@src") if s]
    return hrefs, scripts, iframes


def extract_title(html: str) -> str | None:
    try:
        doc = lxml_html.fromstring(html)
        found = doc.xpath("//title/text()")
        return found[0].strip() if found else None
    except Exception:
        return None


def extract_text(html: str, url: str) -> str:
    """Main text via trafilatura, falling back to a crude strip when it declines.

    trafilatura returns None on pages it considers boilerplate — which includes some perfectly
    good short contact pages, exactly the ones carrying opening hours and a phone number.
    """
    import trafilatura

    extracted = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,  # Swedish clinics put price lists in tables
        favor_recall=True,
        target_language="sv",
    )
    if extracted and extracted.strip():
        return extracted.strip()

    try:
        doc = lxml_html.fromstring(html)
        for bad in doc.xpath("//script | //style | //nav | //footer"):
            bad.getparent().remove(bad)
        return re.sub(r"\n{3,}", "\n\n", doc.text_content()).strip()
    except Exception:
        return ""


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()[:16]


# --- discovery --------------------------------------------------------------------------------


async def discover_from_robots(
    base_url: str, fetcher: Fetcher, cfg: CrawlConfig
) -> tuple[RobotFileParser | None, list[str], str | None]:
    """Parse robots.txt for Disallow rules and Sitemap: directives."""
    robots_url = urljoin(base_url, "/robots.txt")
    result = await fetcher.fetch(robots_url)
    if not result.ok or not result.html:
        return None, [], None

    parser = RobotFileParser()
    parser.parse(result.html.splitlines())
    sitemaps = [
        line.split(":", 1)[1].strip()
        for line in result.html.splitlines()
        if line.lower().startswith("sitemap:")
    ]
    return parser, sitemaps, result.html


async def discover_from_sitemap(
    sitemap_url: str, fetcher: Fetcher, *, depth: int = 0, max_depth: int = 1
) -> list[str]:
    """URLs from a sitemap, recursing one level into a sitemap index."""
    result = await fetcher.fetch(sitemap_url)
    if not result.ok or not result.html:
        return []

    body = result.html
    if sitemap_url.endswith(".gz"):
        try:
            body = gzip.decompress(body.encode("latin-1")).decode("utf-8", "replace")
        except Exception:
            return []

    try:
        root = lxml_html.fromstring(body.encode("utf-8"))
    except Exception:
        return []

    locs = [t.strip() for t in root.xpath("//*[local-name()='loc']/text()") if t.strip()]
    is_index = bool(root.xpath("//*[local-name()='sitemapindex']"))

    if is_index and depth < max_depth:
        nested: list[str] = []
        for loc in locs[:10]:  # a handful of child sitemaps is plenty for a clinic
            nested.extend(
                await discover_from_sitemap(loc, fetcher, depth=depth + 1, max_depth=max_depth)
            )
        return nested
    return locs


# --- the crawl ---------------------------------------------------------------------------------


async def crawl(base_url: str, fetcher: Fetcher, cfg: CrawlConfig) -> CrawlResult:
    started = now_utc()
    base_url = base_url.rstrip("/")

    robots, declared_sitemaps, robots_txt = (None, [], None)
    if cfg.respect_robots:
        robots, declared_sitemaps, robots_txt = await discover_from_robots(base_url, fetcher, cfg)

    def allowed(url: str) -> bool:
        if not cfg.respect_robots or robots is None:
            return True
        try:
            return robots.can_fetch(cfg.user_agent, url)
        except Exception:
            return True

    # Sitemap first; BFS only if it yields nothing.
    sitemap_urls: list[str] = []
    for candidate in [*declared_sitemaps, *(urljoin(base_url, p) for p in SITEMAP_CANDIDATES)]:
        if candidate in sitemap_urls:
            continue
        found = await discover_from_sitemap(candidate, fetcher, max_depth=1)
        if found:
            sitemap_urls = found
            logger.info("sitemap %s yielded %d urls", candidate, len(found))
            break

    skipped: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    seen_text: set[str] = set()
    pages: list[Page] = []

    # Priority queue. heapq is a min-heap, so push negated scores.
    counter = 0
    frontier: list[tuple[int, int, str, int]] = []

    def push(url: str, depth: int) -> None:
        nonlocal counter
        normalized = normalize_url(url, base_url)
        if normalized is None:
            if is_pdf(url):
                # Swedish clinics often publish the price list as a PDF. v1 does not read them,
                # but review.py must be able to tell the operator to check prices by hand.
                skipped.append((url, "pdf"))
            return
        if normalized in seen_urls:
            return
        if cfg.same_domain_only and not same_site(
            normalized, base_url, include_subdomains=cfg.include_subdomains
        ):
            skipped.append((normalized, "off-domain"))
            return
        if not allowed(normalized):
            skipped.append((normalized, "robots"))
            return
        score = url_priority(normalized, base_url)
        if score == 0:
            skipped.append((normalized, "low-value"))
            return

        seen_urls.add(normalized)
        counter += 1
        heapq.heappush(frontier, (-score, counter, normalized, depth))

    push(base_url + "/", 0)
    for url in sitemap_urls:
        push(url, 1)

    sem = asyncio.Semaphore(cfg.concurrency)

    async def fetch_one(url: str) -> FetchResult:
        async with sem:
            return await fetcher.fetch(url)

    while frontier and len(pages) < cfg.max_pages:
        # Drain a concurrency-sized batch off the top of the queue.
        batch: list[tuple[int, str, int]] = []
        while frontier and len(batch) < cfg.concurrency and len(pages) + len(batch) < cfg.max_pages:
            neg_score, _, url, depth = heapq.heappop(frontier)
            batch.append((-neg_score, url, depth))

        results = await asyncio.gather(*(fetch_one(url) for _, url, _ in batch))

        for (score, url, depth), result in zip(batch, results, strict=True):
            if not result.ok:
                skipped.append((url, result.error or f"http {result.status}"))
                continue
            if not result.is_html:
                skipped.append((url, "not-html"))
                continue

            text = extract_text(result.html or "", result.final_url)
            words = len(text.split())
            if words < cfg.min_words:
                skipped.append((url, "too-short"))
                continue

            fingerprint = text_fingerprint(text)
            if fingerprint in seen_text:
                # /priser and /priser/ served as separate URLs, or a print variant.
                skipped.append((url, "duplicate-content"))
                continue
            seen_text.add(fingerprint)

            links, scripts, iframes = extract_links(result.html or "", result.final_url)
            pages.append(
                Page(
                    url=url,
                    final_url=result.final_url,
                    depth=depth,
                    title=extract_title(result.html or ""),
                    text=text,
                    links=links,
                    script_srcs=scripts,
                    iframe_srcs=iframes,
                    word_count=words,
                    priority=score,
                    fetched_at=now_utc(),
                )
            )

            if depth < cfg.max_depth:
                for link in links:
                    push(link, depth + 1)

    pages.sort(key=lambda p: (-p.priority, p.url))
    return CrawlResult(
        base_url=base_url,
        pages=pages,
        skipped=skipped,
        robots_txt=robots_txt,
        sitemap_urls=sitemap_urls,
        started_at=started,
        finished_at=now_utc(),
    )
