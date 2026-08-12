# Teknisk spec — Demo: AI-reception + sandbox + ingestion
**Version 0.1 · Augusti 2026 · Intern**

Målet: en demo per klinik som (1) byggs automatiskt från klinikens webbplats, (2) låter klinikchefen ringa och prata med agenten live, (3) visar hela stacken i en dashboard (bokning, SMS, risk, återbudsfyllning, matchning) — utan någon riktig systemintegration. Sandboxen använder samma kanoniska datamodell som produktionen, så adaptern mot Kaddio/Muntra ersätter bara datakällan senare.

---

## 1. Arkitektur

```
[Kliniksajt] --crawl--> [Ingestion pipeline] --clinic_profile.json--> [Seed-generator]
                                                                          |
                                                                          v
[Telefon] <--> [Vapi/Retell agent] <--function calls--> [Sandbox API (FastAPI)]
                                                          |          |
                                              [Postgres/Supabase]  [46elks SMS]
                                                          |
                                             [WebSocket events] --> [Demo-dashboard (React)]
```

Ett `clinic_id` per demo. Allt (agentprompt, sandboxdata, dashboard) genereras från `clinic_profile`.

---

## 2. Datamodell (Postgres DDL)

Kanonisk modell — branschneutral, samma i produktion.

```sql
create table clinics (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  website_url text,
  profile jsonb not null,          -- hela clinic_profile från ingestion
  demo_phone text,                  -- numret agenten svarar på
  created_at timestamptz default now()
);

create table resources (            -- behandlare, rum
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  kind text check (kind in ('practitioner','room','equipment')) default 'practitioner',
  name text not null,               -- från scrapen: riktiga behandlarnamn!
  title text                        -- 'Leg. fysioterapeut'
);

create table services (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  name text not null,               -- 'Nybesök fysioterapi'
  duration_min int not null,
  price_sek numeric,                -- null om okänt; agenten säger då "kontakta oss"
  category text,                    -- 'fysio' | 'massage' | ...
  bookable boolean default true
);

create table service_resources (    -- vilka behandlare utför vilken tjänst
  service_id uuid references services(id),
  resource_id uuid references resources(id),
  primary key (service_id, resource_id)
);

create table slots (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  resource_id uuid references resources(id),
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  status text check (status in ('open','booked','blocked')) default 'open'
);
create index on slots (clinic_id, status, starts_at);

create table patients (             -- ENBART FEJKADE i demo
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  name text, phone text,            -- fejknummer utom "JA-svararen" (er egen mobil)
  is_demo boolean default true,
  noshow_risk numeric default 0.1   -- 0..1, sätts av riskmodellen
);

create table bookings (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  slot_id uuid references slots(id),
  service_id uuid references services(id),
  patient_id uuid references patients(id),
  status text check (status in ('confirmed','cancelled','completed','noshow')) default 'confirmed',
  booked_via text,                  -- 'voice' | 'refill' | 'seed'
  risk numeric,                     -- snapshot av no-show-risk vid bokning
  created_at timestamptz default now()
);

create table waitlist (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid references clinics(id),
  patient_id uuid references patients(id),
  service_id uuid references services(id),
  earliest timestamptz, latest timestamptz,
  short_notice_ok boolean default true,
  created_at timestamptz default now()
);

create table events (               -- åtgärdsloggen = dashboardens hjärta + framtida audit log
  id bigserial primary key,
  clinic_id uuid references clinics(id),
  ts timestamptz default now(),
  type text not null,               -- se §6
  payload jsonb not null,
  sek_value numeric                 -- 'räddad intäkt' för refill-events
);
```

**Seed-regler** (körs från `clinic_profile`): generera slots för innevarande + nästa vecka, mån–fre 08–17, per behandlare; boka ~80 % med fejkpatienter; lämna 3–5 öppna luckor på "bra" demotider (idag em, imorgon fm); skapa 8–10 väntelisteposter varav **en pekar på er egen mobil** (JA-svararen); sätt `noshow_risk` med heuristiken i §5.

---

## 3. Sandbox-API (FastAPI)

### Agent-verktyg (anropas av Vapi/Retell via function calling)
| Endpoint | Metod | Gör |
|---|---|---|
| `/agent/{clinic_id}/availability` | POST | `{service_name?, resource_name?, date_range}` → topp 3 lediga slots, formulerade för uppläsning ("imorgon kl 10 hos Johanna") |
| `/agent/{clinic_id}/book` | POST | `{slot_id, service_id, patient_name, phone}` → skapar patient+bokning, skickar SMS-bekräftelse, emitterar event |
| `/agent/{clinic_id}/reschedule` | POST | `{booking_ref, new_slot_id}` → ombokar, SMS, event |
| `/agent/{clinic_id}/cancel` | POST | `{booking_ref}` → avbokar, **triggar refill-motorn**, event |
| `/agent/{clinic_id}/waitlist` | POST | lägger patient på väntelista |
| `/agent/{clinic_id}/knowledge` | POST | `{question}` → RAG-svar ur `clinic_profile.faq` + policies. Svar innehåller `answer` + `confidence`; låg confidence → agenten säger "det ska jag be en kollega återkomma om" |
| `/agent/{clinic_id}/escalate` | POST | loggar eskalering, returnerar frasen agenten ska säga |

Alla svar: `{speech: "text att säga", data: {...}}` — agenten läser `speech`, dashboarden får `data` via event.

### Demo-regi (anropas från dashboarden)
| Endpoint | Gör |
|---|---|
| `POST /sim/{clinic_id}/cancel_random` | avbokar en seedad bokning → refill-kaskaden startar |
| `POST /sim/{clinic_id}/waitlist_reply` | simulerar att patient N svarar JA efter `delay_s` (default 20 s). OBS: JA-svararen med er riktiga mobil svarar på riktigt via SMS-webhook — använd sim-endpointen som fallback |
| `POST /sim/{clinic_id}/reset` | återställer sandboxen till seed-läget (körs mellan möten!) |

### Refill-motorn (kärnlogik, ~100 rader)
1. `cancel` → slot blir `open`, event `slot_freed` (dashboard: lucka blinkar röd).
2. Kandidater = waitlist där service matchar, tidsfönster täcker sloten, `short_notice_ok`.
3. Ranka: väntetid + risk (lågriskpatient först). Skicka SMS #1: "Hej {name}! En tid för {service} har blivit ledig {tid}. Svara JA så är den din."
4. SMS-webhook (46elks) tar emot "JA" → boka, bekräfta via SMS, event `slot_refilled {sek_value: service.price_sek}`. Timeout 90 s → nästa kandidat.

### Dashboard
`GET /dash/{clinic_id}/state` (initial), `WS /dash/{clinic_id}/events` (live).

---

## 4. Röstagenten

**Verktygsdefinitioner:** spegla §3 rakt av (JSON schema per verktyg). Håll parametrarna få — LLM:en klantar sig med många.

**Systemprompt-mall (genereras per klinik):**
```
Du är den digitala receptionisten för {clinic.name} i {city}.
ÖPPNING (obligatorisk, exakt): "Hej, du har kommit till {clinic.name}.
Du pratar med vår digitala assistent – jag kan hjälpa dig boka, omboka
eller svara på frågor. Vad kan jag hjälpa dig med?"

REGLER:
1. Svara KORT (max 2 meningar per tur). Läs aldrig upp mer än 3 alternativ.
2. Du får boka/omboka/avboka och svara på praktiska frågor via verktygen.
3. MEDICINSK GRÄNS: Du bedömer ALDRIG symtom, ger ALDRIG råd om behandling
   eller läkemedel. Vid frågor om symtom: "Det kan jag tyvärr inte bedöma –
   för rådgivning ring 1177." Vid ord som bröstsmärta, andnöd, svimning,
   kraftig blödning: anropa escalate och säg "Det låter som något du ska
   söka vård för direkt – ring 112 om det är akut, annars 1177. Vill du
   att en kollega ringer upp dig?"
4. Bekräfta aldrig uppgifter om befintliga bokningar utan att först be om
   namn + telefonnummer som bokningen gjordes med. (I skarp drift: BankID.)
5. Vet du inte svaret (knowledge confidence låg): lova återkoppling,
   hitta aldrig på. Särskilt: hitta ALDRIG på priser.
6. Prata svenska, varmt och effektivt. Sammanfatta bokningen innan du
   bekräftar: tjänst, tid, behandlare, pris.

KUNSKAP OM KLINIKEN:
{compact_profile_json}   # tjänster+priser, behandlare, öppettider, policies
```

**Röststack:** Retell eller Vapi · ElevenLabs svensk röst (testa 2–3, välj lägst latens) · Deepgram Nova (sv) · LLM: snabb modell, EU-endpoint även i demo (vana + säljpoäng). Mål: < 1,5 s svarslatens. Trick: `availability` returnerar färdigformulerad `speech` så LLM:en slipper formatera tider.

---

## 5. Ingestion-pipelinen

**Crawl:** sitemap.xml → fallback interna länkar (max djup 2, max 40 sidor, endast samma domän, respektera robots.txt, aldrig bakom inloggning). Rendera med Playwright (många kliniksajter är JS). Extrahera huvudtext per sida (trafilatura/readability).

**Extraktion:** en LLM-körning per sajt med schemat nedan; skicka sidorna chunkade med URL-kontext.

```json
{
  "clinic_profile": {
    "name": "", "city": "", "address": "", "phone": "", "email": "",
    "booking_system_hint": "",        // 'kaddio'|'muntra'|'bokadirekt'|... (leta i länkar/skript! styr er adapterpitch)
    "opening_hours": [{"day":"mon","open":"08:00","close":"17:00"}],
    "staff": [{"name":"","title":"","specialties":[""]}],
    "services": [{"name":"","duration_min":0,"price_sek":null,"category":"","description":""}],
    "policies": {"cancellation":"","late_fee":"","insurance_accepted":[""],"referral_needed":null},
    "practical": {"parking":"","transit":"","accessibility":""},
    "faq": [{"q":"","a":"","source_url":""}],
    "confidence_notes": ""            // LLM:ens egna osäkerheter — GRANSKA MANUELLT
  }
}
```

Regler i extraktionsprompten: hitta aldrig på priser/tider (null om osäkert); varje faq-post ska ha source_url; skriv a-fälten som talspråk (agenten läser upp dem).

**Manuell QA-grind (5 min per klinik, obligatorisk):** granska priser, behandlarnamn, avbokningspolicy innan demon. Fel pris i demo = död affär.

**Risk-heuristik (demo-nivå, ärligt presenterad som regelmodell):**
`risk = 0.08 + 0.15*(bokad >21 dgr i förväg) + 0.10*(måndag 8-9 el. fredag 15-17) + 0.20*(ny patient) + 0.15*("tidigare no-show")` — clampa 0..0.85. Färger i dashboard: <0.2 grön, <0.45 gul, annars röd + flagga "bekräftelsekrav".

---

## 6. Event-typer (dashboardens språk)

`call_started` · `call_transcript {partial}` · `booking_created` · `booking_rescheduled` · `booking_cancelled` · `slot_freed` · `refill_sms_sent {candidate_n}` · `refill_reply {answer}` · `slot_refilled {sek_value}` · `escalation` · `sms_sent` · `match_network {from_clinic, to_clinic}` (skriptad för matchningsvyn)

Dashboard-paneler: Kalender (slots färgade) · Livesamtal (transkript) · Riskpanel (veckans bokningar) · Återbudsflöde (kaskad-tidslinje + SEK-räknare) · Nätverksvy (skriptad ring 2-animation) · Händelselogg.

---

## 7. Checklista (7 dagar)

- [ ] **D1:** DDL + seed-generator + `reset`-endpoint. Ingestion v1 (crawl+extraktion) körd mot 3 riktiga sajter.
- [ ] **D2:** Agent-endpoints + refill-motor + 46elks (skicka/webhook). Enhetstesta kaskaden.
- [ ] **D3:** Vapi/Retell-agent med verktyg + dynamisk prompt. Första hela samtalet mot sandbox.
- [ ] **D4:** Latens-tuning svenska, edge-fall (avbrott, tystnad, "prata med människa"), eskaleringsflödet.
- [ ] **D5:** Dashboard: state + WS, kalender, livesamtal, SEK-räknare.
- [ ] **D6:** Riskpanel + nätverksvy + `cancel_random`-regi + klinik-PDF-generatorn ("Så här skulle {klinik}s digitala reception se ut" + QR till demonumret).
- [ ] **D7:** Generalrep x2 (en utomstående). Spela in backup-video. `reset` verifierad. Ärlighetsfraserna i manus ("sandlåda, i pilot kopplar vi ert system").

**Definition of done:** en kall klinik-URL → körbar, granskad demo på < 30 min varav < 10 min manuellt arbete.