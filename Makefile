.PHONY: install db-up db-down db-shell migrate revision run test lint fmt seed reset crawl review dash dash-build demo voice voice-push

install:
	uv sync
	uv run playwright install chromium
	cd dashboard && npm install

db-up:
	@docker info >/dev/null 2>&1 || { echo 'Docker daemon is not running. Start Docker Desktop.'; exit 1; }
	docker compose up -d
	@echo 'Waiting for Postgres...'
	@until docker compose exec -T db pg_isready -U vakans -q; do sleep 1; done
	@echo 'Postgres ready on localhost:55432'

db-down:
	docker compose down

db-shell:
	docker compose exec db psql -U vakans -d vakans

migrate:
	uv run alembic -c backend/alembic.ini upgrade head

# usage: make revision m="add foo"
revision:
	uv run alembic -c backend/alembic.ini revision --autogenerate -m "$(m)"

run:
	uv run uvicorn app.main:app --reload --app-dir backend

test:
	uv run ruff check .
	uv run ruff format --check .
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

seed:
	uv run python -m app.cli seed

reset:
	uv run python -m app.cli reset

# usage: make crawl url=https://klinik.se slug=klinik
crawl:
	uv run vakans-review crawl "$(url)" --slug "$(slug)"

# usage: make review slug=klinik
review:
	uv run vakans-review show "$(slug)"

# Regenerate the ElevenLabs agent config from the clinic profile.
#   usage: make voice [slug=demo] [url=https://<tunnel>]
voice:
	uv run python -m app.cli voice --slug "$(or $(slug),demo)" $(if $(url),--base-url "$(url)",)

# Same, and upload it. Needs VAKANS_ELEVENLABS_API_KEY + VAKANS_ELEVENLABS_AGENT_ID.
voice-push:
	uv run python -m app.cli voice --slug "$(or $(slug),demo)" $(if $(url),--base-url "$(url)",) --push

# The Next.js dashboard. Needs `make run` in another terminal — it reads the API on :8000.
dash:
	cd dashboard && npm run dev

dash-build:
	cd dashboard && npm run build

# Everything a demo needs, in one terminal each:
#   make db-up && make migrate && make seed
#   make run          (terminal 1: API + refill worker)
#   make dash         (terminal 2: dashboard on :3000)
demo:
	@echo 'Terminal 1: make run'
	@echo 'Terminal 2: make dash    ->  http://localhost:3000'
	@echo ''
	@echo 'Then, from the dashboard header:'
	@echo '  "Avboka en tid"  -> a slot goes amber, SMS #1 goes out, 90s countdown starts'
	@echo '  wait 90s         -> the cascade advances to candidate #2 on its own'
	@echo '  "Svara JA"       -> slot refills, "Raddad intakt" ticks up'
	@echo '  "Aterstall"      -> back to a clean sandbox between meetings'
