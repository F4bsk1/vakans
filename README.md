# Vakans

AI reception + capacity matching for Swedish private clinics. Demo-first: a cold clinic URL
becomes a runnable, personalised demo — voice agent, cancellation-refill engine, live dashboard.

See [docs/PROJECT.md](docs/PROJECT.md) for the charter and
[docs/Demo_Teknisk_Spec.md](docs/Demo_Teknisk_Spec.md) for the build spec.

## Runbook

    brew install uv && uv python install 3.12
    cp .env.example .env          # then edit VAKANS_FOUNDER_MOBILE
    make install
    make db-up
    make migrate
    make reset                    # seeds the demo sandbox, prints what is armed

App on http://localhost:8000 (`/healthz`, `/docs`). Postgres on `localhost:55432`.

### Cold clinic URL → demo

    uv run vakans-review run https://someclinic.se --slug someclinic
    uv run vakans-review approve someclinic --reviewer "Your Name"
    uv run vakans-review load someclinic

`approve` is the mandatory QA gate: an unreviewed profile cannot be seeded or demoed.

## Layout

    backend/app/         FastAPI, canonical models, seed generator, events
    backend/ingestion/   crawl → extract → validate → review CLI
    backend/alembic/     migrations
    dashboard/           Next.js (step 5)
    docs/                charter, build spec, schema notes

## Stack

- FastAPI, SQLAlchemy 2 and Alembic on Postgres 16 (Docker locally, Supabase EU for demos)
- Pydantic AI for the typed agent tools; ElevenLabs Agents for the voice layer
- Playwright, trafilatura and instructor for clinic-website ingestion
- 46elks for SMS, with a `fake` provider for both LLM and SMS so the whole suite runs offline
- Next.js dashboard with plain CSS Modules

## Tests

    make test                      # ruff + pytest; integration tests need `make db-up`
    uv run pytest tests/unit -q    # unit tests only, no database
