# Schema notes — where the code deviates from the spec, and why

Companion to [Demo_Teknisk_Spec.md](Demo_Teknisk_Spec.md) §2. The spec's DDL is the canonical
starting point and is followed table-for-table. This file records the **eight additive deltas**
in `backend/app/models.py` / migration `0001_canonical_schema`, plus what we deliberately did
*not* build.

Nothing here removes or renames a spec column. Every delta is additive, so the spec DDL remains
a truthful description of the model.

**Review this list before step 3.** It is the one set of decisions that is expensive to revisit
once the agent endpoints and refill engine are written on top.

---

## The eight deltas

### G1 — Nothing prevented double-booking a slot
`bookings.slot_id` had no uniqueness constraint. Two concurrent writes — the voice agent
booking a slot while an inbound refill "JA" claims the same one — would both read
`slot.status='open'` and both insert. The dashboard would then show two patients in one slot.

This is not hypothetical: the demo *deliberately choreographs* that race.

    create unique index uq_bookings_active_slot on bookings (slot_id) where status = 'confirmed';

Partial on purpose, so a cancelled booking correctly frees the slot for a new one. The loser of
the race gets an `IntegrityError` and the agent says "tiden hann tyvärr bli bokad". Step 3
should additionally `select … from slots where id = :id for update` inside the booking
transaction, turning the exception path into a clean lock.

Tested: `test_double_booking_is_rejected`, `test_cancelled_booking_frees_the_slot_again`.

### G2 — `booking_ref` was used by the API but defined nowhere
Spec §3's `/agent/reschedule` and `/agent/cancel` both take a `booking_ref`, but §2 has no such
column. Seeded bookings need one too, because the demo caller phones about a booking the seed
created.

Added `bookings.booking_ref` with `unique (clinic_id, booking_ref)`. Six characters from a
**voice-safe alphabet** (`app/booking_ref.py`): no `0/O`, no `1/I/L`, no `U/V` — the pairs that
Swedish TTS→STT round-trips mangle over a phone codec.

Note that prompt rule 4 in spec §4 requires the agent to ask for name + phone before touching
an existing booking, so the *primary* lookup in step 3 is `(clinic_id, patient.phone, status)`
— indexed — and `booking_ref` is the reference the patient reads back off their SMS.

### G3 — The refill cascade had nowhere to live
Spec §2's `waitlist` holds only preferences. The engine in §3 needs to know which candidates
were offered, in what order, when the 90 s timeout expires, and which offer an inbound "JA"
belongs to.

- `waitlist.status` ∈ `active | offered | fulfilled | expired | cancelled`, plus `priority`.
- New table `refill_offers`, deliberately *not* folded into `waitlist`: a waitlist entry
  receives many offers over its lifetime, and one cascade fans out across many entries.
  `cascade_id` groups a whole cascade and doubles as the `correlation_id` the dashboard
  timeline groups on. `unique (cascade_id, candidate_n)`.

### G4 — No SMS storage at all
Spec §3 has 46elks send + inbound webhook and §6 has an `sms_sent` event, but §2 stores
nothing. Events alone are insufficient: **46elks retries inbound webhooks**, so we need an
idempotency key, and an inbound "JA" must resolve to a live offer.

New table `sms_messages` with a partial unique index on `(provider, provider_message_id)`.
Only `sms_messages.refill_offer_id` exists, not the reverse pointer, so the two tables stay
acyclic and Alembic doesn't need `use_alter`.

### G5 — The risk heuristic's inputs were not columns
Spec §5's formula needs "ny patient" and "tidigare no-show". `patients` had neither, so
`risk.py` could not be fed from the database and the seed could not produce a believable
red/yellow/green spread for the step-6 risk panel.

- `patients.prior_noshows`, `patients.first_seen_at`
- `unique (clinic_id, phone) where phone is not null` — **without this, `/agent/book` mints a
  duplicate patient on every call from the same number**, which destroys the "ny patient"
  signal outright. Partial, because walk-ins may have no number.
- `bookings.booked_at` as the business timestamp the lead-time term reads. `created_at` stays
  row-creation, so a reschedule doesn't silently reset the ">21 days ahead" signal.

### G6 — `slots.status` had no `held`
During the 90 s SMS offer window the slot must be reserved, or the voice agent can book it out
from under a running cascade — which is exactly the moment the demo is built around. Defining
the enum as `open | booked | blocked | held` now costs nothing; changing a CHECK constraint
later is a real migration.

### G7 — `clinics` lacked identity and locale fields
Added `slug` (unique; the CLI, artifacts dir and dashboard route all want one), `timezone`
(defaulting to Europe/Stockholm — never hardcode it in a path that may one day serve a Finnish
clinic), `sms_sender_id`, `profile_reviewed_at` / `profile_reviewed_by`, `seeded_at`,
`seed_fingerprint`, `updated_at`.

`profile_reviewed_at` is load-bearing: it is what turns spec §5's "obligatorisk QA-grind" from
a recommendation into something enforced. `/sim/reset` returns **409** for an
ingestion-sourced profile that no human has approved.

### G8 — No delete behaviour, and `service_resources` couldn't be scoped
`ondelete="CASCADE"` on every `clinic_id` FK, so deleting a demo clinic is one statement — which
also makes "we delete everything on request" demonstrably true when a clinic owner asks.

`service_resources` had no `clinic_id`, so a clinic-scoped reset would need a subselect through
`services`. That is easy to get wrong and silently leaks rows across a reset, so the column was
added. Tested: `test_deleting_a_clinic_cascades`.

---

## Smaller additions

- `events` indexes on `(clinic_id, id desc)` and `(clinic_id, type, ts)`. The dashboard tails
  by id; without the first it seq-scans on demo day.
- `events.correlation_id` and `events.actor` (`seed | agent | sim | system | webhook`). The
  actor column is what makes the charter's audit-log claim actually true, and adding
  `correlation_id` in step 5 would mean touching every emitter.
- Bounded numerics: `price_sek numeric(10,2)`, `noshow_risk numeric(4,3)`,
  `bookings.risk numeric(4,3)`, `events.sek_value numeric(12,2)`. Bare `numeric` is unbounded
  and renders `0.30000000000000004`-style noise in the SEK counter.
- `resources.active`, `resources.sort_order` — deterministic calendar ordering.
- `services.duration_is_estimated` — see ambiguity 1 below.
- `slots` CHECK `ends_at > starts_at`, and `patients` CHECK `noshow_risk between 0 and 1`.
- `MetaData(naming_convention=...)`. Six lines. Without it, Alembic autogenerate emits unnamed
  constraints and every future migration becomes a diff-noise fight.

---

## Deferred — written down, not built

- **Slot overlap prevention** via `EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at,
  ends_at) WITH &&) WHERE (status <> 'blocked')`. Needs `CREATE EXTENSION btree_gist`. The seed
  never produces overlaps, so this is a step-3 safety net for reschedule.
- **`calls` table** (call_sid, transcript, recording_url, purge_after) — step 4. This is where
  charter D6's 7-day transcript purge job lives.
- **`knowledge_chunks`** for `/agent/knowledge` — a clinic profile is a few KB, so it goes in
  the prompt. Revisit only if a real clinic's FAQ outgrows the context budget.
- **`bookings.rescheduled_from_id`, `cancelled_at`, `cancel_reason`** — `events` already carries
  this history for the demo.

---

## Spec ambiguities and how they were resolved

1. **`services.duration_min NOT NULL` vs "never invent".** Clinic sites frequently don't publish
   durations, and the extraction prompt forbids guessing. Resolution: keep `NOT NULL` in the
   database, allow `null` in the `ClinicProfile` schema, and have `profile_mapping.py` apply a
   documented per-category default (new visit 45, follow-up 30, massage/aesthetics 60, fallback
   45) while setting `duration_is_estimated = true`. `review.py` surfaces every estimate. This
   keeps the honesty property without a nullable column.
2. **`/sim/reset` (charter §5) vs `/sim/{clinic_id}/reset` (spec §3).** Decision D5 settles it:
   clinic-scoped is authoritative. Two demo meetings the same day must not collide.
3. **"80 % booked" vs "3–5 open slots at good demo times".** These are two separate guarantees,
   not one. Overall fill lands near 80 %, *and* a hand-picked set of slots is force-opened.
4. **The 0–0.85 risk clamp is currently unreachable.** Spec §5's four terms max out at 0.68. The
   clamp is implemented for spec fidelity and future terms, but be aware the demo can never
   display a risk above 0.68.

---

## Deviation from the charter's stack table

Charter §3 names `instructor` for ingestion extraction *and* Pydantic AI for tool-calling.
We use **Pydantic AI for both**. It already does Pydantic-validated structured output with
retries, already provides the provider-agnosticism decision D2 requires, and ships
`TestModel`/`FunctionModel` so extraction tests run with no credentials. Two libraries would
mean two provider switches, two credential paths and two fake-modes.

This is reversible: everything goes through the `ProfileExtractor` Protocol in
`ingestion/llm.py`, so an `InstructorExtractor` slots in without touching callers.
