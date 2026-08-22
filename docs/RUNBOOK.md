# Runbook — running, editing and testing the voice agent

Companion to [ARCHITECTURE.md](ARCHITECTURE.md), which explains *why* the pieces are arranged
this way. This one answers the practical questions: what do I start, where do I change things,
and how do I know it worked.

---

## 1. The four processes

Nothing here is one program. Four things run, and three of them are yours.

| # | What | Command | Port | Needed for |
|---|---|---|---|---|
| 1 | Postgres | `make db-up` | 55432 | everything |
| 2 | API + refill worker | `make run` | 8000 | everything |
| 3 | Dashboard | `make dash` | 3000 | watching the demo |
| 4 | Tunnel | `cloudflared tunnel --url http://localhost:8000` | — | **only** for voice |

The tunnel is the one people forget. Your API listens on `localhost:8000`, which exists only on
your laptop. ElevenLabs' servers are on the internet and cannot see it. `cloudflared` publishes a
temporary public HTTPS address that forwards to your port.

**Its URL changes every time you restart it.** That is the single most common reason a call
suddenly stops working: the tools in ElevenLabs still point at yesterday's hostname. The fix is
always the same — copy the new URL, then `make voice url=<new-url>` and push.

You do not need the tunnel to develop. Only a real phone call needs it.

---

## 2. One turn of a conversation

```
  caller speaks
      │
      ▼
┌─────────────────── ElevenLabs ────────────────────┐
│  speech-to-text                                    │
│  the LLM, holding your prompt + 7 tool definitions │
│  decides: "I need availability"                    │
└──────────────────────┬─────────────────────────────┘
                       │  HTTPS POST, with X-Agent-Token
                       ▼
            https://<random>.trycloudflare.com
                       │
                       ▼
┌──────────────────── your Mac ──────────────────────┐
│  FastAPI  /agent/{clinic_id}/availability          │
│    → checks the token                              │
│    → safety check (medical boundary)               │
│    → queries Postgres                              │
│    → builds a Swedish sentence                     │
│    → writes a row to `events`                      │
│  returns {"speech": "...", "data": {...}}          │
└──────────────────────┬─────────────────────────────┘
                       │
                       ▼
┌─────────────────── ElevenLabs ────────────────────┐
│  the LLM reads `speech` aloud via text-to-speech   │
└──────────────────────┬─────────────────────────────┘
                       ▼
                 caller hears it
```

**Traffic goes one way: ElevenLabs calls you.** You never call them during a conversation, and
there is no persistent connection between you. It is ordinary request/response — they are the
client, you are the server. "Two-way" describes the *conversation*, not the wiring.

Consequences worth internalising:

- You cannot push anything into a call in progress. Anything the agent says must be the answer
  to a tool call it decided to make.
- **You never receive the transcript live.** You see the arguments the model put in a tool
  call — never the caller's sentence. That is the single most important consequence of being
  the server, and it is why `safety.py` is a backstop rather than a filter: it can only screen
  the fields the model happened to fill with the caller's own words (`knowledge.question`,
  `escalate.reason`), not the conversation. The real-time boundary is the prompt's job.
  ElevenLabs' **post-call webhook** delivers the full transcript once the call ends, which is
  where an audit belongs — not blocking, but "how often did the boundary hold?".
- If your laptop sleeps or the API stops, the agent keeps talking — it just gets errors from
  every tool and starts improvising. That failure is quiet and sounds plausible, which is
  exactly why the event log is the thing to trust.

---

## 3. Where do I edit what?

This is the question the split makes confusing. It depends entirely on *what* you are changing.

| What you want to change | Edit this | How it goes live |
|---|---|---|
| How the agent talks, its personality, its rules | `backend/app/agent/prompt.py` | `make voice` + push |
| What a tool actually does (booking logic, matching, prices) | `backend/app/agent/tools.py` | **instantly** — it is your server |
| What the agent says back (Swedish phrasing) | `backend/app/agent/speech.py` | **instantly** |
| Tool names, descriptions, parameters | `backend/app/voice/elevenlabs.py` | `make voice` + push |
| The medical boundary | `backend/app/agent/safety.py` | **instantly** |
| Which voice it speaks with | ElevenLabs dashboard | immediately |
| Which LLM, temperature, turn-taking, interruption | ElevenLabs dashboard | immediately |

**The rule of thumb:** anything about *behaviour and data* is yours and takes effect the moment
you save the file. Anything ElevenLabs needs a *copy* of — the prompt and the tool definitions —
needs a push.

What we generate deliberately excludes voice, LLM and turn-taking. Those are decisions you make
by ear in their dashboard, and a generated file should not silently overwrite them.

### The prompt is generated, not written

Do not edit the prompt in the ElevenLabs dashboard. It is generated per clinic from the reviewed
`clinic_profile`, so editing it there means your change is lost on the next push and does not
exist for clinic #2. Edit `prompt.py`, then push.

`elevenlabs/agent_configs/demo.json` is committed to git, so a change to what the agent says
shows up in a diff.

---

## 4. Where do I test it?

Three levels, cheapest first. Use the cheapest one that can catch the thing you are worried
about.

### Level 1 — no voice at all (seconds)

```bash
make test                                   # 421 tests, no network, no credentials
curl -s -XPOST localhost:8000/agent/$CLINIC/availability \
  -H "X-Agent-Token: $TOKEN" -H 'content-type: application/json' \
  -d '{"service_name":"massage"}' | jq .speech
```

This is the whole tool layer. If the Swedish is wrong or a slot is missing, it is wrong here
too, and finding it here takes a second instead of a phone call.

### Level 2 — talk to it in the browser (a minute)

In the ElevenLabs dashboard, open your agent and use its **test / talk** widget. It uses your
laptop microphone; no phone number needed.

Have the dashboard open on `localhost:3000` next to it. That is the real test surface: you talk,
and you watch rows appear in the event log. The agent can *claim* it booked something using
nothing but its prompt — only a genuine tool call writes a row.

### Level 3 — check what actually happened (seconds)

```bash
uv run python -m app.cli events   --slug demo     # every tool call, in order
uv run python -m app.cli bookings --slug demo --via voice
uv run python -m app.cli cascade  --slug demo     # who got texted, and how it went
tail -f /tmp/vakans-api.log                       # live, including fake SMS bodies
```

`bookings --via voice` is the honest scoreboard. If it is empty, no call has ever produced a
booking, whatever the conversation sounded like.

---

## 5. Deploying: we generate, their CLI pushes

`make voice` writes the config; `@elevenlabs/cli` deploys it. We do not do the HTTP — an earlier
version did, and it was ninety-seven lines duplicating `elevenlabs agents push` that would have
needed maintaining against their API forever.

```
elevenlabs/
  agents.json                    # registry              (tracked in git)
  agent_configs/demo.json        # prompt, first message (tracked in git)
  tool_configs/demo-*.json       # the 7 webhook tools   (gitignored, see below)
```

### One-time setup: tools first, then the agent

The order is not obvious and matters. An agent references its tools *by id*, and those ids only
exist once the tools have been pushed. So the first run is two passes:

```bash
npm install -g @elevenlabs/cli
elevenlabs auth login          # ELEVENLABS_API_KEY. Restrict the key to ElevenAgents: Write.

make voice url=https://<current-tunnel>

cd elevenlabs
elevenlabs tools push          # creates the 7 tools, writes their ids into tools.json
cd .. && make voice url=https://<current-tunnel>   # picks the ids up into the agent config
cd elevenlabs
elevenlabs agents add "Rörelsekliniken Vasastan" --from-file agent_configs/demo.json
```

`vakans voice` prints which pass you are in, so you do not have to remember this.

`agents add` prints the new agent's id. Put it in `VAKANS_ELEVENLABS_AGENT_ID`.

### Every time after that

```bash
make voice url=https://<current-tunnel>
cd elevenlabs
elevenlabs agents push --dry-run            # read this before the real one
elevenlabs agents push
elevenlabs tools push
```

**`make voice` never destroys an id.** Both registries are merged, not overwritten: `tools push`
records ids in `tools.json` and an entry *without* an id means "create", so blindly regenerating
would mint seven fresh tools on every tunnel restart. The ids in the files win over `.env`,
because the files are what the CLI actually did.

### What we deliberately do not generate

No `llm`, no `tts`, no `turn`. Voice, model and turn-taking are chosen by ear in their dashboard,
and a generated file must not silently overwrite a decision made by listening.

`tool_configs/` is gitignored because it embeds the cloudflared URL, which changes on every
tunnel restart and would otherwise put seven modified files in `git status` all day.

---

## 6. When something breaks mid-demo

| Symptom | Almost always |
|---|---|
| Agent invents times, sounds plausible, nothing in the event log | Tools are failing. Check the tunnel URL matches what is in ElevenLabs. |
| "Ett fel uppstod" in the ElevenLabs widget | The agent id or the signed URL, not your code. |
| Tools return 401 | The token in `elevenlabs/tool_configs/*.json` is stale. Re-run `make voice` and `elevenlabs tools push`. |
| Cascade sends SMS #1 then stops | The refill worker is not running. It starts with `make run`; check the log for "refill worker started". |
| Dashboard says "Återansluter" | API restarted. It reconnects on its own with backoff. |
| Dashboard is empty | `make seed` has not been run, or the API is down. |

**Check the tunnel first.** It is the answer more often than everything else combined:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$(grep '^VAKANS_PUBLIC_BASE_URL=' .env | cut -d= -f2-)/healthz"
```

`200` means ElevenLabs can reach you. Anything else — including no answer — means every tool
call is failing and the agent is improvising.

---

## 7. Two computers: what is ours, what is theirs

The confusing part of this architecture is that "the agent" is not one thing in one place. It is
**two computers cooperating**, and almost every question about where to change something is
really the question "which computer owns this?"

```
   ElevenLabs' computer                    your Mac
   ────────────────────                    ────────
   ears    speech-to-text                  memory   Postgres: slots, bookings,
   brain   the LLM + your prompt                    patients, waitlist, events
   mouth   text-to-speech                  hands    the 7 tools: what booking
   timing  who speaks when                          actually means
```

They have **none of our code**. What they hold is a *copy* of exactly two things:

1. the system prompt (a block of Swedish text), and
2. the seven tool definitions (name, description, URL, parameters).

That is the entire contract. Everything else in `backend/` — 1,300 lines of agent logic, the
refill engine, the database — they never see. They just call a URL and read back the `speech`
field we return.

**This is why some changes need a push and some do not.** Change what a booking *does* and it
is live the moment you save the file, because that code runs on your Mac. Change the *prompt*
and you have to send them the new copy.

### Why the split is drawn there on purpose

Charter D1: the voice platform is the piece most likely to be replaced, because it is a
US-framed platform and that stops being acceptable the day real patient data is involved.
Everything of value — the sandbox, the tools, the refill engine, the dashboard — sits below the
line and survives that swap untouched. Only `voice/elevenlabs.py` and `routers/agent.py` would
be rewritten.

---

## 8. What you can tune, on each side

### Their side — everything about *how it sounds and listens*

Set in the ElevenLabs dashboard, or in `conversation_config` if you want it in the config file.
The ones that actually matter for a Swedish clinic:

| Field | What it does | Why it matters here |
|---|---|---|
| `tts.voice_id` | which voice | pick by ear, this is not a config decision |
| `tts.speed`, `tts.stability` | pace and consistency | a receptionist who talks too fast sounds like a robot reading |
| `tts.text_normalisation_type` | how numbers are spoken | **big one** — decides whether "1195 kr" is read as "tolvhundra" or digit-by-digit |
| `tts.pronunciation_dictionary_locators` | custom pronunciations | so "naprapat" and practitioner surnames are said correctly |
| `asr.keywords` | boosts recognition of specific words | **the highest-value knob we are not using.** Feed it the clinic's service names and practitioner names and Swedish clinical vocabulary stops being mis-transcribed |
| `asr.quality` | transcription precision | |
| `turn.turn_timeout` | how long it waits for you to finish | too short and it interrupts a hesitant elderly caller |
| `turn.turn_eagerness` | how quickly it jumps in | |
| `turn.interruption_ignore_terms` | words that do not count as interrupting | so "mm", "ja", "okej" do not cut the agent off mid-sentence |
| `turn.soft_timeout_config` | filler while a tool runs | "ett ögonblick…" while our database query returns — hides latency without us being faster |
| `prompt.llm` | which model | a stronger model needs a shorter prompt |
| `prompt.temperature` | how varied the phrasing | |
| `prompt.timezone` | time awareness | |
| `conversation.max_duration_seconds` | call length cap | |

### Our side — everything about *what is true and what happens*

| What | Where |
|---|---|
| What a booking, cancellation or reschedule actually does | `app/agent/tools.py` |
| The exact Swedish sentence spoken back | `app/agent/speech.py` |
| The medical boundary backstop | `app/agent/safety.py` |
| The agent's brief | `app/agent/prompt.py` |
| The tools' names, descriptions and parameters | `app/voice/elevenlabs.py` |
| Prices, opening hours, policies | the reviewed `clinic_profile`, from the clinic's own website |
| The refill cascade, SMS, the dashboard | `app/refill/`, `app/routers/` |

### One place we deliberately did *not* use their feature

ElevenLabs offers a **knowledge base with RAG** — upload documents, the agent retrieves from
them. We built `knowledge` as a tool instead, answering from the reviewed profile.

The reason is spec §5: *"Fel pris i demo = död affär"*. RAG retrieves a chunk and lets the model
paraphrase it. For opening hours that is fine; for a price it is a coin flip you take on every
call. Our version returns one exact sentence, built from a price that a human approved and that
`scrub_unverified` proved appears verbatim on the clinic's own website.

Worth revisiting for genuinely fuzzy content — treatment descriptions, "what happens at a first
visit" — where paraphrasing is harmless and writing a tool would be silly.

---

### Data residency — check this, it is a charter item

```bash
elevenlabs auth whoami
```

If it says `Residency: Global`, your conversations and transcripts are **not** pinned to the EU.
Fine for a demo with fabricated patients, and exactly the thing that has to change before a
pilot touches a real one (charter §2). ElevenLabs sells EU residency; `elevenlabs auth residency`
switches the CLI to it once the workspace has it enabled.

Worth knowing now rather than discovering it in a procurement conversation.

---

## 9. Constraints — what you cannot do, no matter how you configure it

1. **No live transcript.** You only ever see the arguments the model puts in a tool call. See §2.
2. **You cannot push anything into a call in progress.** Everything the agent says is a reply to
   something it decided to ask you.
3. **The LLM comes from their list** unless you run `prompt.custom_llm` against your own
   endpoint. That is the escape hatch if an EU-hosted model becomes a requirement.
4. **Audio and transcripts sit on their platform.** Fine for a demo with fabricated patients;
   this is the thing that has to change before a pilot handles real ones (charter §2).
5. **The latency floor is theirs.** ASR, inference and TTS are the bulk of a turn. Our share is
   the tool round trip, currently well under 100 ms.

---

## 10. If the goal is "the best possible healthcare receptionist"

In the order the effort actually pays off:

1. **`asr.keywords` with the clinic's vocabulary.** Every mis-heard "naprapat" is a failed call,
   and this is a list of words, not a model change. Highest value for the least work, and we
   already have the list — it is in the clinic profile.
2. **`turn.soft_timeout_config`.** A receptionist who says "ett ögonblick" while looking
   something up sounds human. Silence sounds broken. This buys perceived latency for free.
3. **`turn.interruption_ignore_terms`** for Swedish backchannel — "mm", "javisst", "okej".
   Swedes say these constantly while listening, and each one currently stops the agent talking.
4. **A stronger `prompt.llm`, then re-shorten the prompt.** A scripted prompt is how you
   compensate for a weaker model. Raise the model and remove the compensation together, not
   one without the other.
5. **`tts.text_normalisation_type` and a pronunciation dictionary**, checked against real
   prices and real surnames.
6. **The post-call webhook**, so you can read what actually happened instead of guessing.

Note what is *not* on this list: writing more of our own logic. The tools are the part that
already works.

### Where each of these now lives

1-5 are `conversation_config`, so they are generated by `make voice` and deployed by
`elevenlabs agents push`. Nothing to click:

| Setting | Value we generate | From |
|---|---|---|
| `asr.keywords` | clinic name, every practitioner (whole and split), every service word | the reviewed clinic profile |
| `turn.interruption_ignore_terms` | mm · mhm · ja · javisst · okej · precis · aha · just det … | `SWEDISH_BACKCHANNEL` |
| `turn.merge_with_default_ignore_terms` | `true` — their curated list *as well as* ours | |
| `turn.soft_timeout_config` | after 1.6 s: "Ett ögonblick, jag kollar." (randomised, one per turn, never during the greeting) | `FILLER_MESSAGES` |
| `tts.text_normalisation_type` | `elevenlabs` | see below |
| `prompt.llm` | `claude-sonnet-4-5` | `VAKANS_ELEVENLABS_LLM` |

`text_normalisation_type` deserves its own note. The default, `system_prompt`, asks the *model*
to rewrite numbers as words before speaking — which means it edits our prices on the way out,
the exact thing every other decision here exists to prevent. The `elevenlabs` setting normalises
*after* the model, so "895 kronor" is still the number we sent.

Two things we deliberately do **not** generate: `tts.voice_id`, because that is chosen by
listening, and `pronunciation_dictionary_locators`, because a dictionary has to be uploaded to
ElevenLabs first and referenced by id — worth doing by hand once you hear a surname mangled.

**6 is not a CLI thing.** It is an endpoint we host, plus a URL you paste into their dashboard:

```
https://<tunnel>/calls/<clinic_id>/post-call?token=<VAKANS_AGENT_TOKEN>
```

Settings → Webhooks in ElevenLabs, type `post_call_transcription`. What it does is in §11.

---

## 11. The post-call audit

This is the only time we ever see what the caller actually said, and it closes the gap in §2.

When a call ends, ElevenLabs POSTs the whole transcript. We do two things with it:

1. Record it as a `call_transcript` event, so the conversation shows on the dashboard.
2. **Re-run the medical boundary over the caller's own turns.** If it finds a red flag and the
   agent never referred them to 1177, that is recorded as an `escalation` with
   `reason: missed_medical_red_flag`.

That second one is the point. Live, `safety.py` can only see the fields the model chose to
forward. Afterwards we can check the real thing. "Here is every call, and every time the
boundary was tested — including the ones the agent got wrong" is a far stronger claim to a
clinic than a regex we promise runs.

**One caveat, deliberately.** ElevenLabs signs deliveries with an `ElevenLabs-Signature` header,
but their docs do not publish the format — they point you at their SDK. Rather than ship guessed
crypto, the endpoint is gated by the same query token as the SMS webhook, and the signature is
stored on the event so HMAC can be added later against a real delivery.
