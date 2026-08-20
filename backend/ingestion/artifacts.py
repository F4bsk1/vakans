"""Read and write per-clinic ingestion artifacts under `artifacts/<slug>/`.

    artifacts/<slug>/
      crawl.json      the CrawlResult, minus raw HTML
      profile.json    spec §5 shaped {"clinic_profile": {...}} plus our meta envelope
      raw/*.html      what the fetcher saw

Keeping the crawl separate from the extraction is what makes prompt tuning bearable: you crawl
a site once and then re-run extraction against the cached pages as many times as you like.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.schemas.clinic_profile import ClinicProfile, ClinicProfileDocument, ProfileMeta
from ingestion.crawl import CrawlConfig, CrawlResult, Page


def clinic_dir(root: Path, slug: str) -> Path:
    return root / slug


def raw_dir(root: Path, slug: str) -> Path:
    return clinic_dir(root, slug) / "raw"


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def save_crawl(root: Path, slug: str, result: CrawlResult) -> Path:
    directory = clinic_dir(root, slug)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "crawl.json"
    path.write_text(
        json.dumps(
            {
                "base_url": result.base_url,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "robots_txt": result.robots_txt,
                "sitemap_urls": result.sitemap_urls,
                "skipped": [list(s) for s in result.skipped],
                "pages": [asdict(p) for p in result.pages],
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    return path


def load_crawl(root: Path, slug: str) -> CrawlResult:
    path = clinic_dir(root, slug) / "crawl.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No crawl for '{slug}'. Run: vakans-review crawl <url> --slug {slug}"
        )
    data = json.loads(path.read_text(encoding="utf-8"))

    def _dt(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    pages = [
        Page(
            url=p["url"],
            final_url=p["final_url"],
            depth=p["depth"],
            title=p.get("title"),
            text=p.get("text", ""),
            links=p.get("links", []),
            script_srcs=p.get("script_srcs", []),
            iframe_srcs=p.get("iframe_srcs", []),
            word_count=p.get("word_count", 0),
            priority=p.get("priority", 10),
            fetched_at=_dt(p.get("fetched_at")),
        )
        for p in data.get("pages", [])
    ]
    return CrawlResult(
        base_url=data["base_url"],
        pages=pages,
        skipped=[tuple(s) for s in data.get("skipped", [])],
        robots_txt=data.get("robots_txt"),
        sitemap_urls=data.get("sitemap_urls", []),
        started_at=_dt(data.get("started_at")),
        finished_at=_dt(data.get("finished_at")),
    )


def save_profile(root: Path, slug: str, doc: ClinicProfileDocument) -> Path:
    directory = clinic_dir(root, slug)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profile.json"
    # Spec-shaped on disk, so an artifact is directly comparable to spec §5.
    payload = doc.profile.to_spec_json()
    payload["meta"] = json.loads(doc.meta.model_dump_json())
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_profile(root: Path, slug: str) -> ClinicProfileDocument:
    path = clinic_dir(root, slug) / "profile.json"
    if not path.exists():
        raise FileNotFoundError(f"No profile for '{slug}'. Run: vakans-review extract {slug}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return ClinicProfileDocument(
        profile=ClinicProfile.from_spec_json(data),
        meta=ProfileMeta.model_validate(data.get("meta", {"source": "ingestion"})),
    )


def profile_exists(root: Path, slug: str) -> bool:
    return (clinic_dir(root, slug) / "profile.json").exists()


def crawl_config_from_settings(settings) -> CrawlConfig:
    return CrawlConfig(
        max_pages=settings.crawl_max_pages,
        max_depth=settings.crawl_max_depth,
        respect_robots=settings.crawl_respect_robots,
        user_agent=settings.crawl_user_agent,
    )
