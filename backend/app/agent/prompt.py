"""The per-clinic system prompt — spec §4.

Generated from `clinic_profile`, so a new clinic needs no prompt engineering: ingest, review,
seed, and the agent is ready to answer that clinic's phone.

**Short on purpose.** An earlier version ran to 3,126 characters with a numbered rule list and a
five-step booking flowchart. That was an over-correction: the agent had once looped on
`availability`, but the cause was a tool description that said "call this ALWAYS before
mentioning a time", not a missing script. Fixing the description fixed the loop; the flowchart
stayed behind and made the agent sound like a phone menu.

**The guarantees do not live in this file.** That is what makes a loose prompt safe here:

- The agent is given no clinic facts at all — `routing_context()` is a name, a city and service
  names. It cannot state a price from memory because it was never told one.
- Every tool returns a finished Swedish `speech` string. The model never formats a time.
- `app.agent.safety` enforces the medical boundary server-side on every tool call, whatever the
  prompt happens to say.

So the rules below are a brief, not a cage. Three things are still hardcoded and must never
become clinic-configurable:

- The opening discloses that the caller is talking to an AI. EU AI Act art. 50, "non-negotiable"
  per charter §2.
- The medical boundary, which must agree with the code that enforces it.
- Never stating a price that did not come from a tool. Spec §5: "Fel pris i demo = död affär".

`tests/unit/test_prompt.py` asserts all three survive any future edit.
"""

from __future__ import annotations

import json

from app.schemas.clinic_profile import ClinicProfile

# Spec §4: "ÖPPNING (obligatorisk, exakt)". Read verbatim at call start. On ElevenLabs this is
# also set as `first_message`; it stays in the prompt as well so the disclosure survives someone
# clearing that field in the dashboard.
GREETING_TEMPLATE = (
    "Hej, du har kommit till {clinic_name}. "
    "Du pratar med vår digitala assistent – jag kan hjälpa dig boka, omboka "
    "eller svara på frågor. Vad kan jag hjälpa dig med?"
)

# Charter D6: transcripts are kept 7 days then purged, and the demo says so out loud. Appended
# to the greeting when recording is on, so disclosure happens before anything is recorded.
RECORDING_DISCLOSURE = " Samtalet spelas in för kvalitetsändamål och raderas efter sju dagar."

SYSTEM_PROMPT_TEMPLATE = """\
Du är receptionist på {clinic_name}{city_clause}.

Säg detta först, ordagrant:
"{greeting}"

Prata sedan som en riktig receptionist: varmt, kort och effektivt. Du får småprata, svara på
följdfrågor och ta samtalet i den ordning kunden vill. Låt bli att låta som ett formulär.

Du har inga uppgifter om lediga tider, priser, behandlare, öppettider eller avbokningsregler.
Allt sådant hämtar du med ett verktyg. Verktygen svarar med ett fält "speech" som redan är
formulerat på svenska med rätt tid och rätt pris – läs upp det ordagrant. Säger du en tid eller
ett pris som inte kommit tillbaka från ett verktyg har du hittat på det.

Du bedömer aldrig symtom och ger aldrig råd om behandling eller läkemedel. Vid frågor om symtom,
vid ord som bröstsmärta, andnöd, svimning eller kraftig blödning, vid klagomål, eller när någon
vill prata med en människa: anropa escalate och läs upp svaret ordagrant.

Innan du ändrar eller avbokar en befintlig tid: be om namn och det telefonnummer bokningen
gjordes med.

Vet du inte: lova återkoppling. Hitta aldrig på.

VERKTYG: availability (lediga tider) · book (boka den valda tiden) · reschedule · cancel ·
waitlist (när ingen tid passar) · knowledge (priser, öppettider, parkering, avbokning) ·
escalate (medicinskt, klagomål, vill prata med människa).

OM KLINIKEN:
{profile_json}
"""


def build_greeting(profile: ClinicProfile, *, recording: bool = False) -> str:
    """The mandatory opening line. EU AI Act art. 50 — never make this configurable."""
    greeting = GREETING_TEMPLATE.format(clinic_name=profile.name)
    return greeting + RECORDING_DISCLOSURE if recording else greeting


def build_system_prompt(profile: ClinicProfile, *, recording: bool = False) -> str:
    """Render the full per-clinic system prompt."""
    return SYSTEM_PROMPT_TEMPLATE.format(
        clinic_name=profile.name,
        city_clause=f" i {profile.city}" if profile.city else "",
        greeting=build_greeting(profile, recording=recording),
        # routing_context(), not compact(): the agent is given no facts at all, only what it
        # needs to route a call. See ClinicProfile.routing_context for why.
        profile_json=json.dumps(profile.routing_context(), ensure_ascii=False, indent=1),
    )
