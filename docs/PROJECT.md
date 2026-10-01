# PROJECT.md — Vakans (working name): AI reception + capacity matching for Swedish clinics

> Purpose of this file: single source of truth for the project. Read this before writing any code.
> Last updated: 2026-08-12. Owner: Fabian + co-founder.

## 1. What we are building (context)

A demo-first product for Swedish private healthcare clinics (physio, naprapathy, aesthetics first):

1. **AI front desk ("skills suite")** — voice agent that answers the clinic's phone in Swedish: books, reschedules, cancels, answers practical questions (prices, parking, hours). Knowledge auto-ingested from the clinic's own website.
2. **Matching engine** — when a slot is freed (cancellation), fill it within minutes from the waiting list via SMS cascade. Later: match across clinics + insurance demand. No-show risk scoring drives differentiated reminders.
3. **Demo dashboard** — live view shown to clinic owners: calendar, live call transcript, SMS events, risk panel, refill cascade with SEK counter, network/matching preview.

**Demo goal:** cold clinic URL → reviewed, runnable personalized demo in < 30 min (< 10 min manual). Pilot live before Sep 7 (SSE Business Lab deadline).

**No real integrations yet.** A sandbox booking backend simulates the clinic's system, but uses our canonical production data model so real adapters (Kaddio, Muntra, BokaMera) slot in later without touching agent/engine/dashboard code.

## 2. Hard constraints

- **Python everywhere possible.** Only exception: React/Next.js dashboard (customer-facing polish). Everything else Python 3.12+.
- **Relational DB:** Postgres. No document stores.
- **EU data posture from day one** even in demo (habit + sales point): EU-hosted LLM endpoints, EU regions, no training on customer data. Demo uses ONLY fake patient data.
- **EU AI Act art. 50:** agent must disclose it is an AI at call start. Non-negotiable, hardcoded in greeting.
- **Medical boundary:** agent NEVER assesses symptoms, gives medical advice, or handles prescriptions. Keyword escalation (bröstsmärta, andnöd, svimning, blödning → refer 112/1177 + offer human callback). This is a product line, not a nice-to-have.

## 3. Stack (decided — do not relitigate in code sessions)

| Layer | Choice | Notes |
|---|---|---|
| Backend | **FastAPI** (Python 3.12, uv, ruff, pytest) | WebSockets via Starlette for dashboard events |
| DB | **Postgres via Supabase, EU region (Frankfurt)** | Plain SQL/SQLAlchemy Core or asyncpg; do NOT couple business logic to Supabase-specific features except Realtime (see open decision D3). Migration path: Aiven/Scaleway/self-hosted at production |
| ORM/migrations | SQLAlchemy 2.x + Alembic | Keep models mirroring the canonical DDL in Demo_Teknisk_Spec.md §2 |
| Tool-calling layer | **Pydantic AI** (v1) | Typed tools wrapping our own FastAPI endpoints; no LangGraph |
| LLM | **GPT-4.1 via Azure OpenAI Sweden Central** | Non-reasoning mode for live turns. Structured outputs for extraction |
| STT (sv) | **Deepgram Nova-3**, `language=sv`, EU endpoint `api.eu.deepgram.com` | Gladia is the production/patient-data successor — keep STT behind our own interface |
| TTS (sv) | **ElevenLabs Flash v2.5** (NOT v3 — v3 is not realtime) | A/B Cartesia Sonic later; TTS also behind our own interface |
| SMS | **46elks** | Alphanumeric sender ID, delivery + inbound webhooks |
| Ingestion | **Playwright + trafilatura + instructor** (Pydantic-validated LLM extraction) | Schema in Demo_Teknisk_Spec.md §5 |
| Dashboard | **React/Next.js** (the one non-Python part) | Consumes REST + WS from FastAPI |
| Voice orchestration | **OPEN — see decision D1** | Retell (hosted) vs Pipecat (self-hosted Python) |

## 4. Repo layout (target)

```
/backend
  /app
    main.py            # FastAPI app, routers, WS
    models.py          # SQLAlchemy models = canonical data model
    seed.py            # sandbox generator from clinic_profile
    /agent             # Pydantic AI tools: availability, book, reschedule,
                       # cancel, waitlist, knowledge (RAG), escalate
    /refill            # cascade engine + 46elks webhooks
    /risk.py           # heuristic no-show scoring (demo-grade, honest about it)
    /events.py         # event log + WS broadcast (audit log = same table)
    /voice             # adapter interface + retell_adapter.py / pipecat_app.py
  /ingestion
    crawl.py           # sitemap→Playwright→trafilatura
    extract.py         # instructor + clinic_profile schema
    review.py          # CLI to eyeball profile before demo (mandatory QA gate)
  alembic/
/dashboard             # Next.js
/docs
  PROJECT.md           # this file
  Demo_Teknisk_Spec.md # DDL, endpoints, prompts, event types — the build spec
```

Coding conventions: type hints everywhere; every agent tool returns `{speech: str, data: dict}` (backend formats what the agent says — latency + reliability trick); every state change writes to `events` (this table is simultaneously dashboard feed, ROI report, and future compliance audit log — never bypass it).

## 5. Build order (from spec, compressed)

1. DDL + seed generator + `/sim/reset` — sandbox up with fake week from a hardcoded clinic_profile.
2. Ingestion pipeline v1 → real clinic_profile from 3 real clinic sites (incl. `booking_system_hint` detection).
3. Agent tool endpoints + refill cascade + 46elks send/webhook.
4. Voice layer (per decision D1) → first full Swedish call against sandbox.
5. Dashboard: state + WS, calendar, live transcript, SEK counter.
6. Risk panel + network/matching preview + `cancel_random` demo choreography + per-clinic PDF generator.
7. Rehearsals ×2, backup video, reset verified.

## 6. OPEN DECISIONS — resolve these, in order

### D1. Voice orchestration: Retell vs Pipecat (decide before step 4; default = Retell)
**Effort reality check:**
- **Retell (hosted):** agent config + our webhook tools + buy a Swedish number in their dashboard. Telephony, WebRTC, VAD/turn-taking, interruption handling, scaling — all handled. Realistic: **1–2 days** to a good Swedish call. Ongoing ops ≈ zero. Cost ~$0.07/min + components. Tradeoffs: US-framed platform (fine for demo with fake data, NOT fine for patient data), less latency control, per-minute margin forever.
- **Pipecat (self-hosted Python):** we own the pipeline. Work items Retell hides: telephony/SIP or WebRTC transport setup (Daily/Twilio/Telnyx), wiring STT/LLM/TTS services (Pipecat has built-ins, but config + keys + streaming glue), VAD/turn-taking tuning for Swedish (the time sink — interruptions, endpointing, filler handling), hosting a long-running realtime process (not serverless), latency tuning to <1.5s, reconnect/error handling. Realistic for us: **4–7 days** to demo-grade, plus ongoing ops. Payoff: full EU/network control (production requirement for patient data), no platform margin, Python-native.
- **Recommendation:** Retell now (deadline math: Sep 7), but (a) put ALL tool logic in our FastAPI backend so the voice layer is a thin adapter, (b) define `voice/adapter.py` interface day one, (c) schedule Pipecat migration as the first production task when a pilot converts. The sandbox/tools/dashboard are 90% of the code and survive the swap untouched.
- **Kill criterion:** if Retell's Swedish latency/quality embarrasses us in rehearsal (>1.5s turns, garbled sv), eat the Pipecat cost immediately.

### D2. LLM fallback (decide before step 3)
Azure OpenAI Sweden Central requires an Azure subscription + resource approval which can take days. **Action today:** apply for access. Fallback if not approved by step 3: Claude Sonnet via AWS Bedrock eu-north-1/Frankfurt, or Mistral (EU-default). Keep model behind Pydantic AI's provider abstraction so this is a config change.

### D3. Dashboard realtime: Supabase Realtime vs own WS (decide at step 5; default = own WS)
We already run FastAPI; Postgres LISTEN/NOTIFY (or just in-process pub/sub since single instance) + FastAPI WebSocket is ~50 lines, zero vendor coupling, and matches the production path. Use Supabase Realtime only if we hit WS pain under time pressure.

### D4. Telephony number strategy (decide with D1)
Demo number per clinic vs one shared demo number with clinic selection. Default: **one number per demo clinic** (Retell makes this cheap) — the clinic PDF/QR pointing at "their" number is part of the wow.

### D5. Sandbox reset + multi-clinic (decide at step 1)
One DB with `clinic_id` scoping everywhere (already in DDL) + `/sim/reset` per clinic. Confirm: no global state, so two demo meetings the same day can't collide.

### D6. Recording/transcripts retention in demo (decide before first external call)
Even with fake patients, real callers (the clinic owner!) are recorded. Default: transcripts kept 7 days then purged, stated verbally in demo, disclosure line in greeting covers it. Write the purge job early — it's 20 lines and becomes a compliance talking point.

### D7. Name + domain (decide whenever, but before the PDF generator)
"Vakans" is a placeholder. Needed for: sender ID (46elks, 11 chars), PDF branding, demo email.

## 7. Non-goals (do not build these now)

Real EHR integrations (Kaddio/Muntra adapters), BankID flows (mention verbally), real ML risk model (heuristic + honest labeling), insurance bridge (scripted animation only), multi-language, auth/user accounts beyond a dashboard token, Pipecat migration (unless D1 kill criterion triggers).