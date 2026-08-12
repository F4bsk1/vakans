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
