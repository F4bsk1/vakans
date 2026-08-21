# Dashboard

The live view a clinic owner watches during a demo. Next.js 16 (App Router), CSS Modules, no
component library — the whole design system is `app/globals.css` plus `app/dashboard.module.css`.

```bash
make run     # terminal 1: the API on :8000, with the refill worker
make dash    # terminal 2: this, on :3000
```

## How it gets its data

One `GET /dash/{clinic_id}/state` on the server, then one WebSocket. That is the entire
lifecycle.

```
app/page.tsx        Server Component: fetches the snapshot, hands it down as props
lib/useDashboard.ts opens the socket with ?after_id=<snapshot's last_event_id>
                    refetches the snapshot only on events that reshape the board
```

Fetching the snapshot on the server rather than in an effect removes a "laddar…" flash on every
open, and removes a `setState`-inside-`useEffect` that React 19 flags as a cascading render.

The `after_id` is not a detail. Without it, an event committed between the snapshot and the
socket opening is lost, and on stage that looks exactly like the cascade silently skipping a
candidate.

## The rules the design follows

Grounded in the Apple HIG reference set in `~/.claude/skills/apple-design`, adapted for web.
Four of them are load-bearing rather than stylistic:

**Colour never carries meaning alone.** Every status in `lib/vocabulary.ts` has three parts: a
colour token, a **distinct glyph**, and a **Swedish word**. `○ Ledig`, `● Bokad`, `◑ SMS ute`,
`✓ Genomförd`, `✕ Uteblev`. Print the page in greyscale and it still reads correctly; the colour
is reinforcement. This matters more than usual here because red-green is exactly the pairing the
risk bands use, and red-green is the most common colour blindness.

**The system decides light or dark, not the app.** There is deliberately no theme toggle — HIG
is explicit that an app-specific appearance setting makes people think the app is broken when it
ignores their system choice. Every token in `globals.css` has both variants, with the measured
contrast ratio in a comment. The lowest text pair is 4.9:1, against a 4.5:1 requirement.

**Nothing human-readable is assembled in the browser.** Weekday names, clock times and phrases
like "på måndag kl 8 hos Priya" arrive pre-rendered from the backend — the same rule that makes
the voice agent read a server-built `speech` string. A client that never formats a time cannot
format one wrongly, and the dashboard needs no timezone logic. The two exceptions are kronor
(`Intl.NumberFormat`) and the event-log clock, which is explicitly rendered in the *clinic's*
timezone rather than the laptop's.

**Motion is optional.** The held slot and the live offer pulse. Under
`prefers-reduced-motion: reduce` they become static, because HIG is blunt about repetitive
animation causing dizziness and, at worst, seizures.

## What running it in a browser changed

Two things no test would have caught, both found by screenshotting the page:

- Every screen-reader sentence was rendering **visibly** in each slot. `.srOnly` lived in
  `globals.css`, but components referenced it as `styles.srOnly` from the CSS *module*, where it
  did not exist — so the class name resolved to `undefined` and no class was applied.
- The calendar opened on the current ISO week. Run the demo on a Friday and the entire calendar
  is history, including the slot the cascade is texting people about right now. It starts from
  today and runs forward.

## Configuration

| Variable | Default | Why |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | Where the API lives. Also derives the WS URL. |
| `NEXT_PUBLIC_DEMO_TOKEN` | unset | Only needed when the API has `VAKANS_DEMO_TOKEN` set. |

The API allows this origin explicitly (`VAKANS_DASHBOARD_ORIGINS`) rather than `*`, because
`/sim/*` is destructive and reachable from any page a browser happens to be on.
