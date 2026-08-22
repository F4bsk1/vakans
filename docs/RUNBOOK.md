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
always the same — copy the new URL, then `make voice-push url=<new-url>`.

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
- You never receive the transcript live. You see *what the agent did*, not what was said.
- If your laptop sleeps or the API stops, the agent keeps talking — it just gets errors from
  every tool and starts improvising. That failure is quiet and sounds plausible, which is
  exactly why the event log is the thing to trust.

---

## 3. Where do I edit what?

This is the question the split makes confusing. It depends entirely on *what* you are changing.

| What you want to change | Edit this | How it goes live |
|---|---|---|
| How the agent talks, its personality, its rules | `backend/app/agent/prompt.py` | `make voice-push` |
| What a tool actually does (booking logic, matching, prices) | `backend/app/agent/tools.py` | **instantly** — it is your server |
| What the agent says back (Swedish phrasing) | `backend/app/agent/speech.py` | **instantly** |
| Tool names, descriptions, parameters | `backend/app/voice/elevenlabs.py` | `make voice-push` |
| The medical boundary | `backend/app/agent/safety.py` | **instantly** |
| Which voice it speaks with | ElevenLabs dashboard | immediately |
| Which LLM, temperature, turn-taking, interruption | ElevenLabs dashboard | immediately |

**The rule of thumb:** anything about *behaviour and data* is yours and takes effect the moment
you save the file. Anything ElevenLabs needs a *copy* of — the prompt and the tool definitions —
needs a push.

A push deliberately writes only the prompt, first message, language and tool list. Voice, LLM
and turn-taking are left alone, because those are decisions you make by ear in their dashboard
and a deploy should not silently overwrite them.

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

## 5. Why `make voice-push` rather than the ElevenLabs CLI?

It is not either/or — `make voice` writes the config in *their* CLI's layout, so both work on
the same files and cannot disagree:

```
elevenlabs/
  agents.json                    # registry              (tracked in git)
  agent_configs/demo.json        # prompt, first message (tracked in git)
  tool_configs/demo-*.json       # the 7 webhook tools   (gitignored — see below)
```

```bash
make voice-push                              # our CLI, over their REST API
cd elevenlabs && elevenlabs agents push      # their CLI, same files
```

`make voice-push` is the daily driver for two reasons: it is one fewer tool to install and log
in to, and it regenerates from the clinic profile — so clinic #2 and #3 need no extra work.
Their CLI is worth having for versioning and branches.

`tool_configs/` is gitignored because it embeds the cloudflared URL, which changes on every
tunnel restart and would otherwise put seven modified files in `git status` all day.

---

## 6. When something breaks mid-demo

| Symptom | Almost always |
|---|---|
| Agent invents times, sounds plausible, nothing in the event log | Tools are failing. Check the tunnel URL matches what is in ElevenLabs. |
| "Ett fel uppstod" in the ElevenLabs widget | The agent id or the signed URL, not your code. |
| Tools return 401 | `VAKANS_AGENT_TOKEN` in `.env` differs from the workspace secret in ElevenLabs. |
| Cascade sends SMS #1 then stops | The refill worker is not running. It starts with `make run`; check the log for "refill worker started". |
| Dashboard says "Återansluter" | API restarted. It reconnects on its own with backoff. |
| Dashboard is empty | `make seed` has not been run, or the API is down. |

**Check the tunnel first.** It is the answer more often than everything else combined:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$(grep '^VAKANS_PUBLIC_BASE_URL=' .env | cut -d= -f2-)/healthz"
```

`200` means ElevenLabs can reach you. Anything else — including no answer — means every tool
call is failing and the agent is improvising.
