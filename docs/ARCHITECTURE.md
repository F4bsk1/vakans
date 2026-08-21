# Architecture — how the voice call actually works

Companion to [PROJECT.md](PROJECT.md) (charter) and [Demo_Teknisk_Spec.md](Demo_Teknisk_Spec.md)
(build spec). Those describe *what* we build; this describes *who runs what* during a live call,
because that split decides our latency budget and our data-residency story.

## Who hosts what

| Runs on ElevenLabs | Runs on our side |
|---|---|
| Microphone/telephony transport (WebRTC, SIP) | Postgres — slots, bookings, patients, waitlist, events |
| Speech-to-text (Swedish ASR) | The seven agent tools (`/agent/{clinic_id}/*`) |
| The conversation LLM (we choose which) | Booking logic, the double-booking lock, risk scoring |
| Turn-taking, interruption, endpointing | The medical-boundary safety check |
| Text-to-speech (Flash v2.5) | The refill cascade and SMS |
| Conversation history for the call | The per-clinic system prompt (generated, then given to them) |

**Audio never reaches our servers.** ElevenLabs holds the microphone stream and the transcript;
we hold the booking data. Two data paths with different risk profiles — see *Data residency*
below.

## One turn, end to end

```
  caller
    │  1. speech (audio)
    ▼
┌─────────────────────── ElevenLabs ───────────────────────┐
│  ASR ──► LLM ──► decides: "I need availability"          │
│                    │                                     │
│                    │ 2. HTTPS POST (tool call)           │
└────────────────────┼─────────────────────────────────────┘
                     ▼
          ┌──── cloudflared tunnel ────┐        (development only)
          │  public https URL          │
          └────────────┬───────────────┘
                       ▼
┌──────────────────── our FastAPI ─────────────────────────┐
│  X-Agent-Token check                                      │
│  safety.check() — medical boundary, before anything else  │
│  query Postgres, format Swedish speech                    │
│  emit an event                                            │
│      │ 3. {"speech": "...", "data": {...}}                │
└──────┼────────────────────────────────────────────────────┘
       ▼
┌─────────────────────── ElevenLabs ───────────────────────┐
│  tool result ──► LLM ──► sentence ──► TTS ──► audio       │
└───────────────────────────┬───────────────────────────────┘
                            ▼  4. speech (audio)
                         caller
```

**The network traffic is one-directional: ElevenLabs calls us.** We never call them during a
call, and there is no persistent socket between us. It is ordinary request/response — they are
the client, we are the server. "Two-way" is true of the *conversation*, not of the connection.

That matters practically: we do not need to hold state between tool calls, we cannot push
anything to a call in progress, and anything we want the agent to say has to be the answer to
a tool call it chose to make.

## Why a Cloudflare tunnel

Our API listens on `localhost:8000`, which is reachable only from the laptop. ElevenLabs'
servers are on the internet and cannot reach it. `cloudflared` publishes a temporary public
HTTPS URL that forwards to that port.

**It is development scaffolding, not part of the product.** In production the API is deployed
somewhere with a real address (an EU host, per the charter's data posture) and there is no
tunnel at all. Nothing in the codebase depends on Cloudflare.

Two consequences while developing:

- The tunnel URL changes on every restart, so the tool URLs in ElevenLabs must be updated. That
  is what `vakans voice --slug <clinic>` regenerates.
- The laptop must be awake with both processes running, or the agent's tool calls fail and it
  falls back to improvising.

## Why tools at all, rather than letting the LLM answer

The LLM is given the clinic's prices and practitioners in its system prompt, so it *can* answer
"vad kostar ett nybesök?" without us. Two reasons it must not:

1. **It has no calendar.** Without a tool call it will invent a plausible time and confirm a
   booking that does not exist. We have watched it do exactly this.
2. **We need the write to be real.** A booking has to survive the call: a row, a slot flipped to
   `booked`, an SMS, an event on the dashboard.

So every tool returns `{speech, data}`. `speech` is a finished Swedish sentence — the backend
renders the date, the price and the practitioner's first name — and the prompt instructs the
model to read it verbatim. `data` is for the dashboard.

This is the charter's latency trick, and it also removes a hallucination class: a model that
never formats a time cannot format one wrongly.

## Where our latency budget is

The charter targets sub-1.5-second turns and kills the vendor choice if we cannot hit it. Of
that budget we control exactly one segment: **step 2 → 3 in the diagram**, the tool round trip.
ASR, LLM inference and TTS are ElevenLabs'.

What we do about our segment:

- Tool handlers are a single indexed query — no N+1, no ORM lazy loads across the wire.
- Swedish phrasing is deterministic string building, no model call.
- Service matching is lexical, not embedding-based: no network hop, no model at request time.

Through the tunnel, a tool call currently returns in well under 100 ms. In production the tunnel
hop disappears.

## Data residency (charter §2)

| Data | Where it lives | Posture |
|---|---|---|
| Caller audio, transcript | ElevenLabs (US-framed) | Fine for a demo with fabricated patients. **Not** fine for real patients. |
| Bookings, patients, events | Our Postgres (EU) | Under our control from day one. |
| Clinic profile, prices | Our Postgres (EU) | Public web data plus staff names. |

This is why `voice/adapter.py` exists and was written on day one, before any vendor was chosen:
the voice platform is the piece most likely to be replaced when a pilot handles real patient
data, and everything below the adapter — sandbox, tools, refill engine, dashboard — is
unaffected by that swap.

## The seam, concretely

```
app/voice/adapter.py       the interface (vendor-neutral)
app/voice/elevenlabs.py    generates ElevenLabs tool definitions + agent config
app/routers/agent.py       thin HTTP shell, ~15 lines per endpoint
app/agent/tools.py         all the logic — no vendor knows this file exists
```

Swapping ElevenLabs for Pipecat, Retell or a self-hosted stack means rewriting the middle two
files. `tools.py`, the database, the refill engine and the dashboard do not change.
