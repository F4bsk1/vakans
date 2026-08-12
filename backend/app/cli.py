"""Operator CLI for the sandbox: `make seed`, `make reset`.

Ingestion has its own CLI (`vakans-review`). This one only deals with the bundled demo clinic
and with resetting sandboxes between meetings.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import unit_of_work
from app.events import EventType, emit
from app.fixtures import load_demo_profile_document
from app.models import CLINIC_SCOPED_DELETE_ORDER, Clinic
from app.seed import SeedConfig, seed_clinic
from app.timeutils import now_utc

app = typer.Typer(help="Vakans sandbox operations", no_args_is_help=True)
console = Console()

DEMO_SLUG = "demo"


def _print_result(clinic: Clinic, result) -> None:
    console.print(
        f"\n[bold green]Sandbox armed[/] for [bold]{clinic.name}[/] ({clinic.slug})"
        f"  fingerprint=[cyan]{result.fingerprint}[/]"
    )

    counts = Table("what", "n", show_header=False, box=None, pad_edge=False)
    for key, value in result.counts.items():
        counts.add_row(key, str(value))
    console.print(counts)

    console.print(
        f"\nRisk bands: [green]{result.risk_band_counts['green']} green[/] · "
        f"[yellow]{result.risk_band_counts['yellow']} yellow[/] · "
        f"[red]{result.risk_band_counts['red']} red[/]"
    )
    console.print(f"Demo slot strategy: [bold]{result.demo_slot_strategy}[/]")
    if result.founder_waitlist_id:
        console.print("JA-svararen is on the waitlist (your own mobile).")
    else:
        console.print(
            "[yellow]No JA-svararen:[/] set VAKANS_FOUNDER_MOBILE so the refill cascade "
            "reaches a phone that actually answers."
        )


@app.command()
def seed(
    slug: str = typer.Option(DEMO_SLUG, help="Clinic slug to create or reuse."),
    demo_phone: str = typer.Option(None, help="The number the agent answers on."),
) -> None:
    """Create (or reuse) the bundled demo clinic and seed its sandbox."""

    async def _run() -> None:
        settings = get_settings()
        doc = load_demo_profile_document()
        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()

            if clinic is None:
                clinic = Clinic(
                    id=uuid4(),
                    name=doc.profile.name,
                    slug=slug,
                    website_url=None,
                    profile=json.loads(doc.model_dump_json()),
                    demo_phone=demo_phone,
                    timezone=settings.default_timezone,
                    sms_sender_id=settings.sms_sender_id,
                )
                session.add(clinic)
                await session.flush()
                console.print(f"Created clinic [bold]{slug}[/]")
            else:
                for model in CLINIC_SCOPED_DELETE_ORDER:
                    await session.execute(delete(model).where(model.clinic_id == clinic.id))
                console.print(f"Reusing clinic [bold]{slug}[/], sandbox cleared")

            now = now_utc()
            result = await seed_clinic(
                session,
                clinic,
                now=now,
                cfg=SeedConfig(founder_phone=settings.founder_mobile),
            )
            clinic.seeded_at = now
            clinic.seed_fingerprint = result.fingerprint
            await emit(
                session,
                clinic_id=clinic.id,
                type=EventType.SIM_RESET,
                payload={"counts": result.counts, "fingerprint": result.fingerprint},
                actor="sim",
            )
            _print_result(clinic, result)

    asyncio.run(_run())


@app.command()
def reset(slug: str = typer.Option(DEMO_SLUG, help="Clinic slug to reset.")) -> None:
    """Re-seed an existing clinic's sandbox. Run this between demo meetings."""
    seed(slug=slug, demo_phone=None)


@app.command("list")
def list_clinics() -> None:
    """Show every clinic and whether its sandbox is seeded."""

    async def _run() -> None:
        async with unit_of_work() as session:
            rows = (await session.execute(select(Clinic).order_by(Clinic.slug))).scalars().all()
            if not rows:
                console.print("[yellow]No clinics yet.[/] Run `make seed`.")
                return
            table = Table("slug", "name", "seeded", "fingerprint", "reviewed")
            for c in rows:
                table.add_row(
                    c.slug,
                    c.name,
                    c.seeded_at.isoformat(timespec="minutes") if c.seeded_at else "-",
                    c.seed_fingerprint or "-",
                    c.profile_reviewed_at.isoformat(timespec="minutes")
                    if c.profile_reviewed_at
                    else "-",
                )
            console.print(table)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
