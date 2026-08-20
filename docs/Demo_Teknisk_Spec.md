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

> **Uppdaterad mot byggd kod (2026-08-20).** Detta är nu den modell vi faktiskt bygger, inte
> v0.1-utkastet. Åtta additiva tillägg gjordes under bygget av steg 1 — de är markerade `[Gn]`
> nedan och motiverade i [SCHEMA_NOTES.md](SCHEMA_NOTES.md). Inget spec-fält togs bort eller
> döptes om.

```sql
-- Speglar backend/app/models.py exakt. Ändra aldrig här utan att också ändra modellen:
-- testet test_migrations_match_models fångar drift mellan modell och migration, men inte
-- mellan modell och det här dokumentet.
-- Rader märkta [Gn] är tillägg mot spec v0.1 — motiv i SCHEMA_NOTES.md.

create table clinics (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  website_url text,
  profile jsonb not null,           -- hela clinic_profile-dokumentet {profile, meta}
  demo_phone text,                  -- numret agenten svarar på
  created_at timestamptz not null default now(),
  slug text not null unique,                                   -- [G7] CLI, artifacts, dashboard
  timezone text not null default 'Europe/Stockholm',           -- [G7] aldrig hårdkodad i kod
  sms_sender_id varchar(11),                                   -- [G7] 46elks, max 11 tecken
  profile_reviewed_at timestamptz,                             -- [G7] QA-grindens spärr
  profile_reviewed_by text,                                    -- [G7]
  seeded_at timestamptz,                                       -- [G7]
  seed_fingerprint text,                                       -- [G7]
  updated_at timestamptz not null default now()                -- [G7]
);

create table resources (            -- behandlare, rum
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,   -- [G8] cascade
  kind text not null default 'practitioner'
    check (kind in ('practitioner','room','equipment')),
  name text not null,               -- från scrapen: riktiga behandlarnamn!
  title text,                       -- 'Leg. fysioterapeut'
  active boolean not null default true,                        -- [G7-ish]
  sort_order int not null default 0                            -- deterministisk kalenderordning
);
create index on resources (clinic_id, sort_order);

create table services (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  name text not null,               -- 'Nybesök fysioterapi'
  duration_min int not null,        -- se ambiguitet 1 i SCHEMA_NOTES.md
  price_sek numeric(10,2),          -- null om okänt; agenten säger då "kontakta oss"
  category text,                    -- 'fysio' | 'massage' | ...
  bookable boolean not null default true,
  duration_is_estimated boolean not null default false         -- gissad längd, visas i review.py
);
create index on services (clinic_id);

create table service_resources (    -- vilka behandlare utför vilken tjänst
  service_id uuid references services(id) on delete cascade,
  resource_id uuid references resources(id) on delete cascade,
  clinic_id uuid not null references clinics(id) on delete cascade,  -- [G8] gör reset scopebar
  primary key (service_id, resource_id)
);
create index on service_resources (clinic_id);

create table slots (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  resource_id uuid not null references resources(id) on delete cascade,
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  -- [G6] 'held' = reserverad under 90 s-fönstret i återbudskaskaden, så röstagenten inte
  -- kan boka bort sloten mitt i kaskaden.
  status text not null default 'open'
    check (status in ('open','booked','blocked','held')),
  updated_at timestamptz not null default now(),
  check (ends_at > starts_at)
);
create index on slots (clinic_id, status, starts_at);
create index on slots (clinic_id, resource_id, starts_at);

create table patients (             -- ENBART FEJKADE i demo
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  name text,
  phone text,                       -- fejknummer utom "JA-svararen" (er egen mobil)
  is_demo boolean not null default true,
  noshow_risk numeric(4,3) not null default 0.100 check (noshow_risk between 0 and 1),
  prior_noshows int not null default 0,                        -- [G5] input till §5-heuristiken
  first_seen_at timestamptz not null default now(),            -- [G5] "ny patient"
  created_at timestamptz not null default now()
);
-- [G5] Utan denna skapar /agent/book en ny patient vid varje samtal från samma nummer, och
-- "ny patient"-signalen i riskmodellen blir meningslös. OBS: kodar in "ett nummer = en person",
-- vilket inte stämmer i verkligheten — se SCHEMA_NOTES.md, "Kända förenklingar".
create unique index on patients (clinic_id, phone) where phone is not null;

create table bookings (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  slot_id uuid not null references slots(id) on delete cascade,
  service_id uuid not null references services(id) on delete cascade,
  patient_id uuid not null references patients(id) on delete cascade,
  status text not null default 'confirmed'
    check (status in ('confirmed','cancelled','completed','noshow')),
  booked_via text,                  -- 'voice' | 'refill' | 'seed'
  risk numeric(4,3),                -- snapshot av no-show-risk vid bokning
  created_at timestamptz not null default now(),
  booking_ref varchar(12) not null,                            -- [G2] används av §3:s API
  booked_at timestamptz not null default now(),                -- [G5] affärstid, ej radskapande
  unique (clinic_id, booking_ref)                              -- [G2]
);
-- [G1] Spec v0.1 tillät två bekräftade bokningar på samma slot. Demon iscensätter medvetet
-- kapplöpningen röstsamtal vs inkommande "JA". Partiellt index, så avbokning frigör sloten.
create unique index on bookings (slot_id) where status = 'confirmed';
create index on bookings (clinic_id, patient_id, status);      -- promptregel 4: namn + telefon
create index on bookings (clinic_id, status);

create table waitlist (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  patient_id uuid not null references patients(id) on delete cascade,
  service_id uuid not null references services(id) on delete cascade,
  earliest timestamptz, latest timestamptz,
  short_notice_ok boolean not null default true,
  created_at timestamptz not null default now(),
  status text not null default 'active'                        -- [G3]
    check (status in ('active','offered','fulfilled','expired','cancelled')),
  priority int not null default 0                              -- [G3]
);
create index on waitlist (clinic_id, status, service_id);

-- [G3] Ett erbjudande per SMS i en kaskad. Medvetet skild från waitlist: en väntelistepost får
-- många erbjudanden över tid, och en kaskad fläktar ut över många poster. cascade_id grupperar
-- hela kaskaden och är samma nyckel som dashboardens tidslinje grupperar på.
create table refill_offers (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  slot_id uuid not null references slots(id) on delete cascade,
  waitlist_id uuid not null references waitlist(id) on delete cascade,
  patient_id uuid not null references patients(id) on delete cascade,
  cascade_id uuid not null,
  candidate_n int not null,
  status text not null default 'sent'
    check (status in ('sent','accepted','declined','expired','superseded')),
  sent_at timestamptz not null default now(),
  expires_at timestamptz not null,  -- 90 s-timeouten
  responded_at timestamptz,
  unique (cascade_id, candidate_n)
);
create index on refill_offers (clinic_id, status);
create index on refill_offers (slot_id);

-- [G4] 46elks återsänder inkommande webhooks, så vi behöver en idempotensnyckel, och ett
-- inkommande "JA" måste kunna knytas till ett levande erbjudande.
create table sms_messages (
  id uuid primary key default gen_random_uuid(),
  clinic_id uuid not null references clinics(id) on delete cascade,
  direction text not null check (direction in ('out','in')),
  to_phone text not null,
  from_phone text,
  body text not null,
  provider text not null default 'fake',
  provider_message_id text,
  status text not null default 'queued'
    check (status in ('queued','sent','delivered','failed','received')),
  refill_offer_id uuid references refill_offers(id) on delete set null,
  patient_id uuid references patients(id) on delete set null,
  created_at timestamptz not null default now()
);
create unique index on sms_messages (provider, provider_message_id)
  where provider_message_id is not null;
create index on sms_messages (clinic_id, created_at);

create table events (               -- åtgärdsloggen = dashboardens hjärta + framtida audit log
  id bigserial primary key,
  clinic_id uuid not null references clinics(id) on delete cascade,
  ts timestamptz not null default now(),
  type text not null,               -- se §6
  payload jsonb not null,
  sek_value numeric(12,2),          -- 'räddad intäkt' för refill-events
  correlation_id uuid,              -- grupperar ett samtal / en kaskad till en tidslinje
  actor text                        -- 'seed' | 'agent' | 'sim' | 'system' | 'webhook'
);
create index on events (clinic_id, id desc);   -- dashboarden tailar på id
create index on events (clinic_id, type, ts);
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