"""The crawler: robots, sitemaps, budget, priority and dedup — all offline."""

from __future__ import annotations

import pytest

from ingestion.crawl import (
    CrawlConfig,
    crawl,
    extract_links,
    normalize_url,
    same_site,
    text_fingerprint,
    url_priority,
)
from tests.fixtures.site_builder import BASE, build, page, simple_site, sitemap, sitemap_index

CFG = CrawlConfig(user_agent="VakansBot/0.1")


# --- URL normalisation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/priser", f"{BASE}/priser"),
        ("/priser/", f"{BASE}/priser"),  # trailing slash is not a distinct page
        ("/priser#topp", f"{BASE}/priser"),
        ("/priser?utm_source=google", f"{BASE}/priser"),
        ("/priser?utm_source=x&sida=2", f"{BASE}/priser?sida=2"),
        ("https://KLINIK.example/Priser", f"{BASE}/Priser"),  # host lowercased, path preserved
        ("/", f"{BASE}/"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw, BASE) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "mailto:info@klinik.se",
        "tel:+46812345678",
        "javascript:void(0)",
        "#anchor",
        "",
        "/logo.png",
        "/prislista.pdf",
        "/style.css",
    ],
)
def test_normalize_url_rejects_non_pages(raw: str) -> None:
    assert normalize_url(raw, BASE) is None


def test_same_site_allows_subdomains_but_not_strangers() -> None:
    assert same_site(f"{BASE}/priser", BASE)
    assert same_site("https://www.klinik.example/x", BASE)
    assert same_site("https://boka.klinik.example/x", BASE)
    assert not same_site("https://facebook.com/klinik", BASE)


# --- priority ------------------------------------------------------------------------------


def test_price_pages_outrank_everything() -> None:
    """40 pages of blog produces a useless profile; the budget must go to prices and staff."""
    assert url_priority(f"{BASE}/priser", BASE) == 100
    assert url_priority(f"{BASE}/behandlingar", BASE) == 90
    assert url_priority(f"{BASE}/om-oss", BASE) == 80
    assert url_priority(f"{BASE}/kontakt", BASE) == 70
    assert url_priority(f"{BASE}/vanliga-fragor", BASE) == 60
    assert url_priority(f"{BASE}/nagot-annat", BASE) == 10


def test_homepage_ranks_high() -> None:
    assert url_priority(f"{BASE}/", BASE) == 95


def test_blog_and_admin_paths_are_excluded_entirely() -> None:
    for path in ("/blogg/nyhet", "/nyheter/2026", "/kategori/rygg", "/wp-json/x", "/feed"):
        assert url_priority(BASE + path, BASE) == 0


def test_swedish_characters_in_paths_are_matched() -> None:
    assert url_priority(f"{BASE}/öppettider", BASE) == 70
    assert url_priority(f"{BASE}/tjänster", BASE) == 90


# --- HTML ----------------------------------------------------------------------------------


def test_extract_links_separates_scripts_and_iframes() -> None:
    html = """<html><body>
      <a href="/priser">Priser</a>
      <script src="https://kaddio.com/widget.js"></script>
      <iframe src="https://bokadirekt.se/embed/123"></iframe>
    </body></html>"""
    hrefs, scripts, iframes = extract_links(html, BASE)
    assert hrefs == [f"{BASE}/priser"]
    assert scripts == ["https://kaddio.com/widget.js"]
    assert iframes == ["https://bokadirekt.se/embed/123"]


@pytest.mark.parametrize("broken", ["", "<html><a href=", "not html at all", "<<<>>>"])
def test_extract_links_never_raises_on_broken_html(broken: str) -> None:
    """Real clinic sites serve some genuinely malformed HTML; one bad page must not kill a crawl."""
    assert extract_links(broken, BASE) == ([], [], [])


def test_extract_links_handles_partially_broken_markup() -> None:
    # lxml recovers what it can rather than giving up on the document.
    hrefs, _, _ = extract_links('<html><body><a href="/priser">P</a><p><div></body>', BASE)
    assert hrefs == [f"{BASE}/priser"]


def test_text_fingerprint_ignores_whitespace_and_case() -> None:
    assert text_fingerprint("Nybesök  895 KR") == text_fingerprint("nybesök 895 kr")


# --- crawling ------------------------------------------------------------------------------


async def test_crawl_finds_the_valuable_pages_and_skips_the_blog() -> None:
    result = await crawl(BASE, simple_site(), CFG)
    urls = {p.url for p in result.pages}

    assert f"{BASE}/priser" in urls
    assert f"{BASE}/personal" in urls
    assert f"{BASE}/kontakt" in urls
    assert f"{BASE}/blogg/nyhet-1" not in urls
    assert (f"{BASE}/blogg/nyhet-1", "low-value") in result.skipped


async def test_pages_come_back_in_priority_order() -> None:
    """Chunk 0 goes to the model with the most attention, so prices must be first."""
    result = await crawl(BASE, simple_site(), CFG)
    assert result.pages[0].url == f"{BASE}/priser"
    assert [p.priority for p in result.pages] == sorted(
        (p.priority for p in result.pages), reverse=True
    )


async def test_robots_disallow_is_respected() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("/priser", "/hemligt")),
            f"{BASE}/priser": page("Priser", "895 kr"),
            f"{BASE}/hemligt": page("Hemligt", "Ska inte hämtas"),
        },
        robots="User-agent: *\nDisallow: /hemligt\n",
    )
    result = await crawl(BASE, fetcher, CFG)
    urls = {p.url for p in result.pages}

    assert f"{BASE}/priser" in urls
    assert f"{BASE}/hemligt" not in urls
    assert (f"{BASE}/hemligt", "robots") in result.skipped


async def test_robots_can_be_disabled_but_defaults_to_on() -> None:
    assert CrawlConfig().respect_robots is True


async def test_sitemap_is_used_when_present() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej"),
            f"{BASE}/sitemap.xml": sitemap((f"{BASE}/priser", f"{BASE}/personal")),
            f"{BASE}/priser": page("Priser", "895 kr"),
            f"{BASE}/personal": page("Personal", "Johanna"),
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert len(result.sitemap_urls) == 2
    assert {f"{BASE}/priser", f"{BASE}/personal"} <= {p.url for p in result.pages}


async def test_sitemap_index_recurses_one_level() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej"),
            f"{BASE}/sitemap.xml": sitemap_index((f"{BASE}/sitemap-1.xml",)),
            f"{BASE}/sitemap-1.xml": sitemap((f"{BASE}/priser",)),
            f"{BASE}/priser": page("Priser", "895 kr"),
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert f"{BASE}/priser" in {p.url for p in result.pages}


async def test_wordpress_sitemap_path_is_tried() -> None:
    """WordPress dominates this market segment and uses /wp-sitemap.xml."""
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej"),
            f"{BASE}/wp-sitemap.xml": sitemap((f"{BASE}/priser",)),
            f"{BASE}/priser": page("Priser", "895 kr"),
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert result.sitemap_urls == [f"{BASE}/priser"]


async def test_bfs_fallback_when_there_is_no_sitemap() -> None:
    result = await crawl(BASE, simple_site(), CFG)
    assert result.sitemap_urls == []
    assert len(result.pages) >= 4


async def test_page_budget_is_enforced() -> None:
    pages = {f"{BASE}/": page("Start", "Hej", links=tuple(f"/sida-{i}" for i in range(30)))}
    for i in range(30):
        pages[f"{BASE}/sida-{i}"] = page(f"Sida {i}", "Innehåll")
    result = await crawl(BASE, build(pages), CrawlConfig(max_pages=5, user_agent="VakansBot/0.1"))
    assert len(result.pages) == 5


async def test_depth_limit_is_enforced() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("/niva-1",)),
            f"{BASE}/niva-1": page("Nivå 1", "Text", links=("/niva-2",)),
            f"{BASE}/niva-2": page("Nivå 2", "Text", links=("/niva-3",)),
            f"{BASE}/niva-3": page("Nivå 3", "Text"),
        }
    )
    result = await crawl(BASE, fetcher, CrawlConfig(max_depth=2, user_agent="VakansBot/0.1"))
    urls = {p.url for p in result.pages}
    assert f"{BASE}/niva-2" in urls
    assert f"{BASE}/niva-3" not in urls


async def test_off_domain_links_are_not_followed() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("https://facebook.com/klinik", "/priser")),
            f"{BASE}/priser": page("Priser", "895 kr"),
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert all("facebook" not in p.url for p in result.pages)
    assert any(reason == "off-domain" for _, reason in result.skipped)


async def test_duplicate_content_is_detected_across_urls() -> None:
    """/priser and /priser/ are commonly served as separate URLs with identical content."""
    body = page("Priser", "Nybesök 895 kr")
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("/priser", "/prislista")),
            f"{BASE}/priser": body,
            f"{BASE}/prislista": body,
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    price_pages = [p for p in result.pages if "pris" in p.url]
    assert len(price_pages) == 1
    assert any(reason == "duplicate-content" for _, reason in result.skipped)


async def test_pdfs_are_recorded_so_the_reviewer_is_warned() -> None:
    """Swedish clinics often publish the price list as a PDF. v1 skips them, but silently
    skipping is how you end up demoing without prices."""
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("/prislista.pdf", "/kontakt")),
            f"{BASE}/kontakt": page("Kontakt", "08-123 45 67"),
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert f"{BASE}/prislista.pdf" in result.skipped_pdfs()


async def test_thin_pages_are_dropped() -> None:
    fetcher = build(
        {
            f"{BASE}/": page("Start", "Hej", links=("/tom",)),
            f"{BASE}/tom": "<html><head><title>Tom</title></head><body><p>Hej</p></body></html>",
        }
    )
    result = await crawl(BASE, fetcher, CFG)
    assert f"{BASE}/tom" not in {p.url for p in result.pages}


async def test_corpus_maps_urls_to_text_for_evidence_checking() -> None:
    """merge.scrub_unverified checks every price quote against this."""
    result = await crawl(BASE, simple_site(), CFG)
    corpus = result.corpus
    assert any("895" in text for text in corpus.values())


async def test_fetch_errors_are_recorded_not_raised() -> None:
    fetcher = build({f"{BASE}/": page("Start", "Hej", links=("/finns-inte",))})
    result = await crawl(BASE, fetcher, CFG)
    assert result.pages  # the homepage still came back
    assert any("/finns-inte" in url for url, _ in result.skipped)


# --- multi-location chains ---------------------------------------------------------------------
# Found by crawling a real site: famlak.se hosts every Familjeläkarna location on one domain,
# so a crawl of /saltsjobaden/ returned Husby's and Vaxholm's fees too. Only 4 of 40 pages were
# the clinic we asked for. Blending six clinics' prices into one profile is worse than a wrong
# price — it is a different clinic's price.

CHAIN = "https://kedjan.example"


def test_path_prefix_is_derived_from_the_url() -> None:
    from ingestion.crawl import path_prefix_for

    assert path_prefix_for("https://klinik.example/") is None
    assert path_prefix_for("https://klinik.example") is None
    assert path_prefix_for(f"{CHAIN}/saltsjobaden/") == "/saltsjobaden"
    assert path_prefix_for(f"{CHAIN}/saltsjobaden") == "/saltsjobaden"


def test_within_prefix_keeps_siblings_out() -> None:
    from ingestion.crawl import within_prefix

    assert within_prefix(f"{CHAIN}/saltsjobaden", "/saltsjobaden")
    assert within_prefix(f"{CHAIN}/saltsjobaden/priser", "/saltsjobaden")
    assert not within_prefix(f"{CHAIN}/husby/priser", "/saltsjobaden")
    # A sibling whose name merely starts the same must not slip through.
    assert not within_prefix(f"{CHAIN}/saltsjobaden-gamla/priser", "/saltsjobaden")
    # No prefix means a single-site clinic: crawl the whole domain.
    assert within_prefix(f"{CHAIN}/anything", None)


async def test_crawl_stays_inside_one_location_of_a_chain() -> None:
    fetcher = build(
        {
            f"{CHAIN}/saltsjobaden": page(
                "Saltsjöbaden",
                "Vår mottagning",
                links=("/saltsjobaden/priser", "/husby/priser", "/vaxholm"),
            ),
            f"{CHAIN}/saltsjobaden/priser": page("Priser", "Nybesök 300 kr"),
            f"{CHAIN}/husby/priser": page("Husby priser", "Nybesök 250 kr"),
            f"{CHAIN}/vaxholm": page("Vaxholm", "En annan mottagning"),
        }
    )
    result = await crawl(
        f"{CHAIN}/saltsjobaden",
        fetcher,
        CrawlConfig(user_agent="VakansBot/0.1", path_prefix="/saltsjobaden"),
    )
    urls = {p.url for p in result.pages}

    assert f"{CHAIN}/saltsjobaden/priser" in urls
    assert f"{CHAIN}/husby/priser" not in urls
    assert f"{CHAIN}/vaxholm" not in urls
    assert any(reason == "outside-clinic-subtree" for _, reason in result.skipped)


def test_the_subtree_root_is_the_clinic_homepage() -> None:
    """It carries the phone number and opening hours; it must not score as a stray page."""
    assert url_priority(f"{CHAIN}/saltsjobaden", f"{CHAIN}/saltsjobaden/") == 95
    assert url_priority(f"{BASE}/", BASE) == 95


# --- staging hosts and news posts ----------------------------------------------------------------


def test_staging_hosts_are_not_crawled() -> None:
    """test.famlak.se appeared in the real sitemap. A profile built from staging content is
    plausible and wrong."""
    assert not same_site("https://test.klinik.example/priser", BASE)
    assert not same_site("https://staging.klinik.example/priser", BASE)
    assert not same_site("https://dev.klinik.example/priser", BASE)
    # Unless the site under test *is* the staging host, which is a legitimate thing to crawl.
    assert same_site("https://test.klinik.example/priser", "https://test.klinik.example")


@pytest.mark.parametrize(
    "slug",
    [
        "nya-patientavgifter-2023-i-region-stockholm",
        "forandring-patientavgift-2017",
        "hojd-avgift-for-uteblivet-besok",
        "kostnadsfri-vaccinering-av-gardasil-for-kvinnor-upp-till-27-ar",
    ],
)
def test_root_level_news_posts_do_not_outrank_the_price_list(slug: str) -> None:
    """All four are real famlak.se URLs that scored 100 — as high as the actual fee page —
    because the headline contains "avgift"."""
    assert url_priority(f"{BASE}/{slug}", BASE) == 10


def test_real_section_pages_keep_their_priority() -> None:
    """The demotion must not catch genuine price pages with hyphenated names."""
    assert url_priority(f"{BASE}/saltsjobaden/familjelakarnas-besoksavgifter", BASE) == 100
    assert url_priority(f"{BASE}/prislista", BASE) == 100
    assert url_priority(f"{BASE}/om-oss", BASE) == 80
