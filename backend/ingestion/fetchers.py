"""How pages are retrieved — the seam that makes the crawler testable.

Everything in `crawl.py` takes a `Fetcher`. That one decision makes robots handling, sitemap
parsing, depth limiting, budget enforcement and dedup fully unit-testable with **no network and
no browser**, which is the difference between a test suite that runs on every commit and one
that never runs at all.

`PlaywrightFetcher` is the real one (many Swedish clinic sites are JS-rendered).
`FixtureFetcher` replays saved HTML in tests. `CachingFetcher` wraps either and writes what it
sees to `artifacts/`, which is where the test fixtures come from in the first place.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from app.timeutils import now_utc

logger = logging.getLogger("vakans.ingestion.fetch")

DEFAULT_TIMEOUT = 20.0
# Clinic sites are small; anything past this is a download, not a page.
MAX_BYTES = 5_000_000

# Swedish cookie-consent walls. Left unclicked, these hide the page body from trafilatura and
# every extracted profile comes back empty for reasons that are very hard to see from the log.
COOKIE_ACCEPT_SELECTORS = (
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "button#onetrust-accept-btn-handler",
    "button[aria-label*='Godkänn' i]",
    "button:has-text('Godkänn alla')",
    "button:has-text('Acceptera alla')",
    "button:has-text('Tillåt alla')",
    "button:has-text('Jag godkänner')",
)


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str | None
    content_type: str
    fetched_at: object = field(default_factory=now_utc)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and bool(self.html)

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...

    async def aclose(self) -> None: ...


class HttpxFetcher:
    """Plain HTTP. Fast, and enough for the many clinic sites that are server-rendered."""

    def __init__(self, *, user_agent: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Language": "sv-SE,sv;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        )

    async def fetch(self, url: str) -> FetchResult:
        try:
            resp = await self._client.get(url)
        except Exception as exc:
            return FetchResult(url, url, 0, None, "", error=f"{type(exc).__name__}: {exc}")

        content_type = resp.headers.get("content-type", "")
        html = resp.text if "html" in content_type or "xml" in content_type else None
        if html is not None and len(html) > MAX_BYTES:
            html = html[:MAX_BYTES]
        return FetchResult(url, str(resp.url), resp.status_code, html, content_type)

    async def aclose(self) -> None:
        await self._client.aclose()


class PlaywrightFetcher:
    """Headless Chromium. Imported lazily so the test suite never needs a browser installed."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = 3,
    ) -> None:
        self._user_agent = user_agent
        self._timeout_ms = int(timeout * 1000)
        self._sem = asyncio.Semaphore(concurrency)
        self._playwright = None
        self._browser = None
        self._context = None

    async def _ensure(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            locale="sv-SE",
            viewport={"width": 1366, "height": 900},
        )
        # Images/fonts/media are pure cost here — we only ever read text. 2-3x faster.
        await self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in ("image", "font", "media")
                else route.continue_()
            ),
        )

    async def fetch(self, url: str) -> FetchResult:
        await self._ensure()
        async with self._sem:
            page = await self._context.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                # Best-effort settle. Swallowed on timeout on purpose: a chat widget that never
                # goes idle would otherwise hang the entire crawl at this line.
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    logger.debug("networkidle timed out for %s, continuing", url)

                await self._dismiss_cookie_wall(page)

                html = await page.content()
                status = resp.status if resp else 0
                content_type = resp.headers.get("content-type", "text/html") if resp else ""
                return FetchResult(url, page.url, status, html, content_type)
            except Exception as exc:
                return FetchResult(url, url, 0, None, "", error=f"{type(exc).__name__}: {exc}")
            finally:
                await page.close()

    async def _dismiss_cookie_wall(self, page) -> None:
        for selector in COOKIE_ACCEPT_SELECTORS:
            try:
                el = page.locator(selector).first
                if await el.count() and await el.is_visible():
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(250)
                    return
            except Exception:
                continue

    async def aclose(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()


class FallbackFetcher:
    """Try the browser, fall back to plain HTTP when it returns nothing useful.

    Some sites block headless Chromium outright. Others render fine without JS. Rather than
    guessing per site, try both — a crawl that silently produces empty pages is much worse than
    one extra request.
    """

    def __init__(self, primary: Fetcher, secondary: Fetcher, *, min_html: int = 500) -> None:
        self._primary = primary
        self._secondary = secondary
        self._min_html = min_html

    async def fetch(self, url: str) -> FetchResult:
        result = await self._primary.fetch(url)
        if result.ok and len(result.html or "") >= self._min_html:
            return result
        logger.info("primary fetcher returned little for %s, falling back", url)
        fallback = await self._secondary.fetch(url)
        return fallback if fallback.ok else result

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._secondary.aclose()


class FixtureFetcher:
    """Replay saved HTML. The whole crawler test suite runs on this.

    Manifest shape: {"pages": {url: {"file": "...", "status": 200, "content_type": "text/html"}}}
    """

    def __init__(self, manifest: dict[str, dict], base_dir: Path) -> None:
        self._pages = manifest.get("pages", manifest)
        self._base_dir = base_dir

    @classmethod
    def from_dir(cls, directory: Path) -> FixtureFetcher:
        import json

        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        return cls(manifest, directory)

    async def fetch(self, url: str) -> FetchResult:
        entry = self._pages.get(url)
        if entry is None:
            return FetchResult(url, url, 404, None, "", error="not in fixture manifest")

        html = entry.get("body")
        if html is None and entry.get("file"):
            html = (self._base_dir / entry["file"]).read_text(encoding="utf-8")
        return FetchResult(
            url,
            entry.get("final_url", url),
            entry.get("status", 200),
            html,
            entry.get("content_type", "text/html; charset=utf-8"),
        )

    async def aclose(self) -> None:
        return None


class CachingFetcher:
    """Wrap a fetcher and write everything it sees to disk.

    Gives free replay (`--from-cache`) and is literally how the test fixtures are produced.
    """

    def __init__(self, inner: Fetcher, cache_dir: Path) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, url: str) -> Path:
        import hashlib

        return self._dir / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.html"

    async def fetch(self, url: str) -> FetchResult:
        result = await self._inner.fetch(url)
        if result.ok and result.html:
            self._path_for(url).write_text(result.html, encoding="utf-8")
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()
