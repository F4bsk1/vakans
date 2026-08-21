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

from app.agent.prompt import build_greeting, build_system_prompt
from app.config import get_settings
from app.db import unit_of_work
from app.events import EventType, emit, recent_events
from app.fixtures import load_demo_profile_document
from app.models import CLINIC_SCOPED_DELETE_ORDER, Booking, Clinic, Patient, Service, Slot
from app.schemas.clinic_profile import ClinicProfile
from app.seed import SeedConfig, seed_clinic
from app.timeutils import get_tz, now_utc
from app.voice import elevenlabs

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


@app.command()
def voice(
    slug: str = typer.Option(DEMO_SLUG, help="Clinic to generate the agent config for."),
    base_url: str = typer.Option(None, help="Public URL ElevenLabs will call. Defaults to config."),
) -> None:
    """Write the ElevenLabs agent config: greeting, system prompt and the seven webhook tools."""

    async def _run() -> None:
        settings = get_settings()
        public = base_url or settings.public_base_url
        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()
            if clinic is None:
                console.print(
                    f"[red]No clinic '{slug}'.[/] Run `make seed` or `vakans-review load`."
                )
                raise typer.Exit(1)

            profile = ClinicProfile.model_validate(clinic.profile["profile"])
            out_dir = settings.artifacts_dir / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            prompt_path = out_dir / "elevenlabs-agent.md"
            prompt_path.write_text(
                f"# ElevenLabs agent — {clinic.name}\n\n"
                f"clinic_id: {clinic.id}\nLanguage: Swedish   TTS: Flash v2.5\n\n"
                f"## First message\n\n{build_greeting(profile)}\n\n"
                f"## System prompt\n\n{build_system_prompt(profile)}\n",
                encoding="utf-8",
            )
            tools_path = out_dir / "elevenlabs-tools.md"
            tools_path.write_text(
                elevenlabs.as_markdown(
                    base_url=public, clinic_id=clinic.id, clinic_name=clinic.name
                ),
                encoding="utf-8",
            )
            json_path = out_dir / "elevenlabs-tools.json"
            json_path.write_text(
                elevenlabs.as_json(base_url=public, clinic_id=clinic.id), encoding="utf-8"
            )

            console.print(f"[green]Agent config[/] -> {prompt_path}")
            console.print(f"[green]Tool setup sheet[/] -> {tools_path}")
            console.print(f"[green]Tool JSON[/] -> {json_path}")
            if public.startswith("http://localhost"):
                console.print(
                    "\n[yellow]VAKANS_PUBLIC_BASE_URL is still localhost.[/] ElevenLabs cannot "
                    "reach that — start a tunnel and re-run with the public URL."
                )
            if settings.agent_token is None:
                console.print(
                    "[yellow]VAKANS_AGENT_TOKEN is not set.[/] /agent/* will refuse every "
                    "request until it is. Generate one: openssl rand -hex 32"
                )

    asyncio.run(_run())


@app.command()
def events(
    slug: str = typer.Option(DEMO_SLUG, help="Clinic to show activity for."),
    limit: int = typer.Option(20, help="How many recent events."),
) -> None:
    """Show what actually happened, from the events table.

    The audit log is the only unfakeable record of a call: the agent can *say* it booked
    something using nothing but its system prompt, but only a real tool call writes a row here.
    """

    async def _run() -> None:
        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()
            if clinic is None:
                console.print(f"[red]No clinic '{slug}'.[/]")
                raise typer.Exit(1)

            rows = await recent_events(session, clinic.id, limit=limit)
            if not rows:
                console.print("[yellow]No events yet.[/] Nothing has called a tool.")
                return

            table = Table("when", "event", "who", "detail")
            for e in rows:
                payload = e.payload or {}
                detail = (
                    payload.get("booking_ref")
                    or payload.get("reason")
                    or payload.get("service")
                    or ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2])
                )
                table.add_row(
                    e.ts.astimezone(get_tz(clinic.timezone)).strftime("%H:%M:%S"),
                    e.type,
                    e.actor or "-",
                    str(detail)[:58],
                )
            console.print(table)

    asyncio.run(_run())


@app.command()
def bookings(
    slug: str = typer.Option(DEMO_SLUG, help="Clinic to inspect."),
    via: str = typer.Option("voice", help="booked_via filter: voice, refill, seed, or all."),
) -> None:
    """List bookings, defaulting to the ones an agent made.

    Use this to verify a booking reference the agent read out loud actually exists.
    """

    async def _run() -> None:
        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()
            if clinic is None:
                console.print(f"[red]No clinic '{slug}'.[/]")
                raise typer.Exit(1)

            stmt = (
                select(Booking, Slot, Patient, Service)
                .join(Slot, Booking.slot_id == Slot.id)
                .join(Patient, Booking.patient_id == Patient.id)
                .join(Service, Booking.service_id == Service.id)
                .where(Booking.clinic_id == clinic.id)
                .order_by(Booking.created_at.desc())
                .limit(20)
            )
            if via != "all":
                stmt = stmt.where(Booking.booked_via == via)

            rows = (await session.execute(stmt)).all()
            if not rows:
                console.print(f"[yellow]No bookings with booked_via={via}.[/]")
                return

            tz = get_tz(clinic.timezone)
            table = Table("ref", "when", "service", "patient", "phone", "risk")
            for b, s, p, svc in rows:
                table.add_row(
                    b.booking_ref,
                    s.starts_at.astimezone(tz).strftime("%a %d %b %H:%M"),
                    svc.name[:24],
                    (p.name or "-")[:20],
                    p.phone or "-",
                    f"{float(b.risk):.2f}" if b.risk is not None else "-",
                )
            console.print(table)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
