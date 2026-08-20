"""`vakans-review` — the mandatory human QA gate from spec §5.

    vakans-review run     <url> --slug <slug>    # crawl -> extract -> check -> show
    vakans-review crawl   <url> --slug <slug>
    vakans-review extract <slug>
    vakans-review show    <slug>                 # THE command: every price with its source
    vakans-review check   <slug>                 # exit 0 clean / 1 blocking
    vakans-review approve <slug> --reviewer "Namn"
    vakans-review load    <slug> [--demo-phone +46...]

`show` and `approve` are what make the gate real rather than aspirational. `approve` is the only
way to set `clinics.profile_reviewed_at`, and `/sim/reset` returns 409 without it — so an
unreviewed profile physically cannot be demoed.

This CLI is on the critical path for the charter's "<10 min manual work" promise. Treat it as
product, not tooling.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select

from app.config import get_settings
from app.db import unit_of_work
from app.events import EventType, emit
from app.models import CLINIC_SCOPED_DELETE_ORDER, Clinic
from app.seed import SeedConfig, seed_clinic
from app.timeutils import now_utc
from ingestion import artifacts
from ingestion.crawl import crawl as run_crawl
from ingestion.crawl import path_prefix_for as run_crawl_prefix
from ingestion.detect import detect_booking_system
from ingestion.extract import EXTRACTION_PROMPT, PartialProfile, extract_profile
from ingestion.fetchers import (
    CachingFetcher,
    FallbackFetcher,
    HttpxFetcher,
    PlaywrightFetcher,
)
from ingestion.llm import PydanticAIExtractor
from ingestion.validate import blocking, is_approvable, validate_profile, warnings

app = typer.Typer(help="Crawl, extract and review clinic profiles", no_args_is_help=True)
console = Console()

EXIT_CLEAN = 0
EXIT_BLOCKED = 1
EXIT_WARNINGS = 2


def _root() -> Path:
    return get_settings().artifacts_dir


def _slug_from_url(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host.split(".")[0] or "klinik"


@app.command()
def crawl(
    url: str = typer.Argument(..., help="Clinic homepage, e.g. https://klinik.se"),
    slug: str = typer.Option(None, help="Short id. Defaults to the domain."),
    no_render: bool = typer.Option(False, "--no-render", help="Skip Playwright, plain HTTP only."),
    max_pages: int = typer.Option(None, help="Override the page budget."),
) -> None:
    """Fetch a clinic site and cache it under artifacts/<slug>/."""
    settings = get_settings()
    slug = slug or _slug_from_url(url)
    cfg = artifacts.crawl_config_from_settings(settings)
    overrides: dict = {}
    if max_pages:
        overrides["max_pages"] = max_pages
    # A chain puts every location on one domain, so a URL with a path means "this clinic",
    # not "this company".
    if (prefix := run_crawl_prefix(url)) is not None:
        overrides["path_prefix"] = prefix
        console.print(f"Begränsar crawlen till [cyan]{prefix}/[/] (klinik i en kedja)")
    cfg = type(cfg)(**{**cfg.__dict__, **overrides})

    async def _run() -> None:
        http = HttpxFetcher(user_agent=settings.crawl_user_agent)
        if no_render:
            fetcher = http
        else:
            # Try the browser, fall back to plain HTTP: some sites block headless Chromium,
            # and a crawl that silently returns empty pages is worse than one extra request.
            fetcher = FallbackFetcher(PlaywrightFetcher(user_agent=settings.crawl_user_agent), http)
        fetcher = CachingFetcher(fetcher, artifacts.raw_dir(_root(), slug))

        with console.status(f"Crawling {url}…"):
            result = await run_crawl(url, fetcher, cfg)
        await fetcher.aclose()

        path = artifacts.save_crawl(_root(), slug, result)
        console.print(
            f"[green]{len(result.pages)} sidor[/] hämtade, {len(result.skipped)} hoppades över "
            f"→ [dim]{path}[/]"
        )
        if pdfs := result.skipped_pdfs():
            console.print(f"[yellow]OBS:[/] {len(pdfs)} PDF hoppades över: {', '.join(pdfs[:3])}")
        for page in result.pages[:8]:
            console.print(f"  [dim]{page.priority:>3}[/] {page.url}")

    asyncio.run(_run())


@app.command()
def extract(
    slug: str = typer.Argument(...),
    provider: str = typer.Option(None, help="Override VAKANS_LLM_PROVIDER for this run."),
) -> None:
    """Run LLM extraction against the cached crawl."""
    settings = get_settings()
    if provider:
        settings = settings.model_copy(update={"llm_provider": provider})

    if settings.llm_provider == "fake":
        console.print(
            "[yellow]VAKANS_LLM_PROVIDER=fake[/] — this produces an empty profile. "
            "Set a real provider (mistral/azure/bedrock) to extract for real."
        )

    async def _run() -> None:
        crawl_result = artifacts.load_crawl(_root(), slug)
        detection = detect_booking_system(crawl_result.pages, base_url=crawl_result.base_url)
        extractor = PydanticAIExtractor(settings, PartialProfile, EXTRACTION_PROMPT)

        with console.status(f"Extracting with {extractor.name}…"):
            doc = await extract_profile(crawl_result, extractor, detection=detection)

        path = artifacts.save_profile(_root(), slug, doc)
        console.print(f"[green]Profil sparad[/] → [dim]{path}[/]")
        console.print(
            f"Modellanrop: {extractor.usage.calls}, "
            f"tokens in/ut: {extractor.usage.input_tokens}/{extractor.usage.output_tokens}"
        )
        _show(slug)

    asyncio.run(_run())


def _show(slug: str) -> list:
    """Render a profile for human review and return its issues."""
    doc = artifacts.load_profile(_root(), slug)
    profile = doc.profile
    try:
        crawl_result = artifacts.load_crawl(_root(), slug)
    except FileNotFoundError:
        crawl_result = None

    issues = validate_profile(doc, crawl_result)

    console.print(
        Panel(
            f"[bold]{profile.name}[/]\n{profile.address or '—'}\n"
            f"Tel: {profile.phone or '—'}   E-post: {profile.email or '—'}\n"
            f"Bokningssystem: [cyan]{profile.booking_system_hint or 'okänt'}[/]",
            title=f"{slug}",
        )
    )

    # The money table: every price next to the URL it came from, so a reviewer can verify one
    # in two seconds. Spec §5: "Fel pris i demo = död affär."
    services = Table("Tjänst", "Längd", "Pris", "Källa", title="Tjänster och priser")
    for s in profile.services:
        if s.price_sek is None:
            price, source = "[dim]okänt[/]", "[dim]—[/]"
        elif s.price_source_url and s.price_evidence:
            price, source = f"{s.price_sek:g} kr", f"[dim]{s.price_source_url}[/]"
        else:
            price, source = f"[red]{s.price_sek:g} kr[/]", "[red]SAKNAR KÄLLA[/]"
        duration = f"{s.duration_min} min" if s.duration_min else "[dim]—[/]"
        services.add_row(s.name, duration, price, source)
    console.print(services)

    staff = Table("Behandlare", "Titel", "Specialiteter", title="Personal")
    for person in profile.staff:
        staff.add_row(person.name, person.title or "—", ", ".join(person.specialties) or "—")
    console.print(staff)

    hours = ", ".join(
        f"{h.day}: {'stängt' if h.closed else f'{h.open}-{h.close}'}" for h in profile.opening_hours
    )
    console.print(f"\n[bold]Öppettider:[/] {hours or '—'}")
    console.print(f"[bold]Avbokning:[/] {profile.policies.cancellation or '—'}")
    if profile.policies.insurance_accepted:
        console.print(f"[bold]Försäkringar:[/] {', '.join(profile.policies.insurance_accepted)}")

    if profile.faq:
        console.print("\n[bold]FAQ[/] [dim](läses upp av agenten)[/]")
        for item in profile.faq:
            console.print(f"  [cyan]{item.q}[/]\n    {item.a}\n    [dim]{item.source_url}[/]")

    blockers, warns = blocking(issues), warnings(issues)
    if blockers:
        console.print("\n[bold red]Blockerande problem[/]")
        for issue in blockers:
            console.print(f"  [red]✗[/] {issue.message}")
    if warns:
        console.print("\n[bold yellow]Att kontrollera[/]")
        for issue in warns:
            console.print(f"  [yellow]![/] {issue.message}")
    if not issues:
        console.print("\n[green]Inga anmärkningar.[/]")

    return issues


@app.command()
def show(slug: str = typer.Argument(...)) -> None:
    """Print the profile for review — prices in red have no verified source."""
    _show(slug)


@app.command()
def check(
    slug: str = typer.Argument(...),
    strict: bool = typer.Option(False, "--strict", help="Exit non-zero on warnings too."),
) -> None:
    """Validate without printing the whole profile. Exit 0 clean, 1 blocking, 2 warnings."""
    doc = artifacts.load_profile(_root(), slug)
    try:
        crawl_result = artifacts.load_crawl(_root(), slug)
    except FileNotFoundError:
        crawl_result = None

    issues = validate_profile(doc, crawl_result)
    for issue in issues:
        colour = "red" if issue.blocking else "yellow"
        console.print(f"[{colour}]{issue.severity.value}[/] {issue.code}: {issue.message}")

    if blocking(issues):
        raise typer.Exit(EXIT_BLOCKED)
    if strict and issues:
        raise typer.Exit(EXIT_WARNINGS)
    console.print("[green]OK[/]")


@app.command()
def approve(
    slug: str = typer.Argument(...),
    reviewer: str = typer.Option(..., help="Who checked it. Recorded on the profile."),
) -> None:
    """Sign off a profile. This is the only way to unlock it for a demo."""
    doc = artifacts.load_profile(_root(), slug)
    try:
        crawl_result = artifacts.load_crawl(_root(), slug)
    except FileNotFoundError:
        crawl_result = None

    issues = validate_profile(doc, crawl_result)
    if not is_approvable(issues):
        console.print("[red]Kan inte godkännas — åtgärda först:[/]")
        for issue in blocking(issues):
            console.print(f"  [red]✗[/] {issue.message}")
        raise typer.Exit(EXIT_BLOCKED)

    doc.meta.reviewed_at = now_utc()
    doc.meta.reviewed_by = reviewer
    artifacts.save_profile(_root(), slug, doc)
    console.print(f"[green]Godkänd[/] av {reviewer}. Kör: vakans-review load {slug}")


@app.command()
def load(
    slug: str = typer.Argument(...),
    demo_phone: str = typer.Option(None, help="The number the agent answers on."),
) -> None:
    """Upsert the reviewed clinic into the database and seed its sandbox."""
    doc = artifacts.load_profile(_root(), slug)
    if not doc.is_reviewed:
        console.print(
            f"[red]Inte granskad.[/] Kör: vakans-review show {slug} && "
            f'vakans-review approve {slug} --reviewer "Ditt namn"'
        )
        raise typer.Exit(EXIT_BLOCKED)

    settings = get_settings()

    async def _run() -> None:
        payload = doc.profile.to_spec_json()
        payload = {
            "profile": payload["clinic_profile"],
            "meta": json.loads(doc.meta.model_dump_json()),
        }

        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()

            if clinic is None:
                clinic = Clinic(
                    id=uuid4(),
                    name=doc.profile.name,
                    slug=slug,
                    website_url=doc.meta.base_url,
                    profile=payload,
                    demo_phone=demo_phone,
                    timezone=settings.default_timezone,
                    sms_sender_id=settings.sms_sender_id,
                )
                session.add(clinic)
                await session.flush()
            else:
                for model in CLINIC_SCOPED_DELETE_ORDER:
                    from sqlalchemy import delete

                    await session.execute(delete(model).where(model.clinic_id == clinic.id))
                clinic.name = doc.profile.name
                clinic.profile = payload
                clinic.website_url = doc.meta.base_url
                if demo_phone:
                    clinic.demo_phone = demo_phone

            # Mirror the artifact's sign-off onto the row the reset gate actually checks.
            clinic.profile_reviewed_at = doc.meta.reviewed_at
            clinic.profile_reviewed_by = doc.meta.reviewed_by

            now = now_utc()
            result = await seed_clinic(
                session, clinic, now=now, cfg=SeedConfig(founder_phone=settings.founder_mobile)
            )
            clinic.seeded_at = now
            clinic.seed_fingerprint = result.fingerprint
            await emit(
                session,
                clinic_id=clinic.id,
                type=EventType.INGESTION_COMPLETED,
                payload={
                    "slug": slug,
                    "counts": result.counts,
                    "reviewed_by": doc.meta.reviewed_by,
                },
                actor="sim",
            )

            console.print(
                f"[green]{doc.profile.name}[/] laddad och seedad "
                f"(fingerprint {result.fingerprint}, {result.counts['slots']} tider)."
            )

    asyncio.run(_run())


@app.command()
def run(
    url: str = typer.Argument(...),
    slug: str = typer.Option(None),
    provider: str = typer.Option(None),
) -> None:
    """crawl -> extract -> show. The one-command path from a cold URL to something reviewable."""
    slug = slug or _slug_from_url(url)
    crawl(url=url, slug=slug, no_render=False, max_pages=None)
    extract(slug=slug, provider=provider)
    console.print(
        f"\nNästa steg: granska ovan, sedan "
        f'[bold]vakans-review approve {slug} --reviewer "Ditt namn"[/]'
    )


if __name__ == "__main__":
    app()
