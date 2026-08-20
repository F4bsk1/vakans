"""Build a small in-memory clinic site for crawler tests.

Inline HTML rather than files on disk: the crawler tests are about robots, sitemaps, budget,
priority and dedup, and those read better when the site under test is visible in the test file.
Real cached HTML from actual clinics lives in tests/fixtures/sites/<slug>/ and is used for
extraction tests instead.
"""

from __future__ import annotations

from ingestion.fetchers import FixtureFetcher

BASE = "https://klinik.example"


def page(title: str, body: str, *, links: tuple[str, ...] = (), extra: str = "") -> str:
    nav = "".join(f'<a href="{href}">{href}</a>' for href in links)
    # ~40 words of filler so pages clear the min_words threshold the way real ones do.
    filler = " ".join(["Vi erbjuder behandling och rehabilitering för dig som har ont."] * 6)
    return f"""<!doctype html>
<html lang="sv"><head><title>{title}</title></head>
<body><nav>{nav}</nav><main><h1>{title}</h1><p>{body}</p><p>{filler}</p></main>{extra}</body></html>"""


def sitemap(urls: tuple[str, ...]) -> str:
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset>{entries}</urlset>'


def sitemap_index(children: tuple[str, ...]) -> str:
    entries = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex>{entries}</sitemapindex>'


def build(pages: dict[str, str], *, robots: str | None = None) -> FixtureFetcher:
    manifest: dict[str, dict] = {"pages": {}}
    for url, body in pages.items():
        content_type = "application/xml" if url.endswith(".xml") else "text/html; charset=utf-8"
        if url.endswith("robots.txt"):
            content_type = "text/plain"
        manifest["pages"][url] = {"body": body, "status": 200, "content_type": content_type}

    if robots is not None:
        manifest["pages"][f"{BASE}/robots.txt"] = {
            "body": robots,
            "status": 200,
            "content_type": "text/plain",
        }
    return FixtureFetcher(manifest, base_dir=None)  # type: ignore[arg-type]


def simple_site(**overrides: str) -> FixtureFetcher:
    """A believable little clinic site: homepage, prices, staff, contact, and a blog to ignore."""
    pages = {
        f"{BASE}/": page(
            "Startsida",
            "Välkommen till kliniken.",
            links=("/priser", "/personal", "/kontakt", "/blogg/nyhet-1"),
        ),
        f"{BASE}/priser": page("Prislista", "Nybesök 895 kr. Återbesök 695 kr."),
        f"{BASE}/personal": page("Vår personal", "Johanna Lindqvist, leg. fysioterapeut."),
        f"{BASE}/kontakt": page("Kontakt", "Öppettider vardagar 08-17. Telefon 08-123 45 67."),
        f"{BASE}/blogg/nyhet-1": page("Nyhet", "Vi har flyttat."),
    }
    pages.update(overrides)
    return build(pages)
