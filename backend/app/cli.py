"""Operator CLI for the sandbox: `make seed`, `make reset`.

Ingestion has its own CLI (`vakans-review`). This one only deals with the bundled demo clinic
and with resetting sandboxes between meetings.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import delete, func, select

from app.agent.prompt import build_greeting, build_system_prompt
from app.config import get_settings
from app.db import unit_of_work
from app.events import EventType, emit, recent_events
from app.fixtures import load_demo_profile_document
from app.models import (
    CLINIC_SCOPED_DELETE_ORDER,
    Booking,
    Clinic,
    Event,
    Patient,
    RefillOffer,
    Service,
    Slot,
)
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
    """Generate the ElevenLabs agent config from this clinic's reviewed profile.

    Writes `elevenlabs/` in the layout `@elevenlabs/cli` reads, plus a markdown sheet under
    `artifacts/<slug>/` for entering it by hand. Deploying is their CLI's job:

        cd elevenlabs && elevenlabs agents push --dry-run   # see what would change
        cd elevenlabs && elevenlabs agents push

    Re-run this whenever the prompt, the tools or the tunnel URL changes.
    """

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
            prompt = build_system_prompt(profile)
            greeting = build_greeting(profile)

            # --- config as code, in the official CLI's layout ---------------------------
            cfg_dir = Path("elevenlabs")
            (cfg_dir / "agent_configs").mkdir(parents=True, exist_ok=True)
            (cfg_dir / "tool_configs").mkdir(parents=True, exist_ok=True)

            _write_json(
                cfg_dir / "agents.json",
                elevenlabs.agents_registry(slug=slug, agent_id=settings.elevenlabs_agent_id),
            )
            _write_json(
                cfg_dir / "agent_configs" / f"{slug}.json",
                elevenlabs.agent_config(
                    clinic_name=clinic.name, prompt=prompt, first_message=greeting
                ),
            )
            for payload in elevenlabs.tool_payloads(base_url=public, clinic_id=clinic.id):
                name = payload["tool_config"]["name"]
                _write_json(cfg_dir / "tool_configs" / f"{slug}-{name}.json", payload)

            # --- the fallback sheet, for entering it by hand -----------------------------
            out_dir = settings.artifacts_dir / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "elevenlabs-agent.md").write_text(
                f"# ElevenLabs agent — {clinic.name}\n\n"
                f"clinic_id: {clinic.id}\nLanguage: Swedish   TTS: Flash v2.5\n\n"
                f"## First message\n\n{greeting}\n\n"
                f"## System prompt\n\n{prompt}\n",
                encoding="utf-8",
            )
            (out_dir / "elevenlabs-tools.md").write_text(
                elevenlabs.as_markdown(
                    base_url=public, clinic_id=clinic.id, clinic_name=clinic.name
                ),
                encoding="utf-8",
            )

            console.print(f"[green]Config as code[/] -> {cfg_dir}/  ({len(prompt)} char prompt)")
            console.print(f"[green]Setup sheet[/]    -> {out_dir}/elevenlabs-agent.md")

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

            console.print("\nDeploy it with their CLI:")
            if settings.elevenlabs_agent_id:
                console.print("  [bold]cd elevenlabs && elevenlabs agents push --dry-run[/]")
                console.print("  [bold]cd elevenlabs && elevenlabs agents push[/]")
            else:
                # No id yet, so `push` has nothing to update. `add --from-file` creates the
                # agent from our config and records the id it gets back.
                console.print(
                    f'  [bold]cd elevenlabs && elevenlabs agents add "{clinic.name}" '
                    f"--from-file agent_configs/{slug}.json[/]"
                )
                console.print(
                    "  [dim]then put the id it prints in VAKANS_ELEVENLABS_AGENT_ID "
                    "and re-run this[/]"
                )

    asyncio.run(_run())


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
def cascade(
    slug: str = typer.Option(DEMO_SLUG, help="Clinic to inspect."),
    limit: int = typer.Option(10, help="How many offers."),
) -> None:
    """Show the refill cascade: who was texted, in what order, and how it went.

    The one command to run when a demo does not do what you expected. `sent` with a clock in
    the future means it is still waiting; `sent` with a clock in the past means the worker is
    not running (check that `make run` is up and VAKANS_REFILL_WORKER_ENABLED is not false).
    """

    async def _run() -> None:
        async with unit_of_work() as session:
            clinic = (
                await session.execute(select(Clinic).where(Clinic.slug == slug))
            ).scalar_one_or_none()
            if clinic is None:
                console.print(f"[red]No clinic '{slug}'.[/]")
                raise typer.Exit(1)

            rows = (
                await session.execute(
                    select(RefillOffer, Patient, Slot)
                    .join(Patient, RefillOffer.patient_id == Patient.id)
                    .join(Slot, RefillOffer.slot_id == Slot.id)
                    .where(RefillOffer.clinic_id == clinic.id)
                    .order_by(RefillOffer.sent_at.desc(), RefillOffer.candidate_n)
                    .limit(limit)
                )
            ).all()
            if not rows:
                console.print(
                    "[yellow]No refill offers.[/] Free a slot first: "
                    f"curl -XPOST localhost:8000/sim/{clinic.id}/cancel_random"
                )
                return

            tz = get_tz(clinic.timezone)
            colours = {
                "accepted": "green",
                "sent": "cyan",
                "expired": "yellow",
                "declined": "yellow",
                "superseded": "dim",
            }
            table = Table("#", "cascade", "patient", "slot", "status", "window closes")
            for offer, patient, slot in rows:
                status = str(offer.status.value)
                table.add_row(
                    str(offer.candidate_n),
                    str(offer.cascade_id)[:8],
                    (patient.name or "-")[:22],
                    slot.starts_at.astimezone(tz).strftime("%a %d %b %H:%M"),
                    f"[{colours.get(status, 'white')}]{status}[/]",
                    offer.expires_at.astimezone(tz).strftime("%H:%M:%S"),
                )
            console.print(table)

            counter = (
                await session.execute(
                    select(func.coalesce(func.sum(Event.sek_value), 0)).where(
                        Event.clinic_id == clinic.id, Event.type == "slot_refilled"
                    )
                )
            ).scalar_one()
            console.print(f"\nRecovered revenue: [bold green]{counter:,.0f} kr[/]")

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
