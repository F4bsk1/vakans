.PHONY: install db-up db-down db-shell migrate revision run test lint fmt seed reset crawl review

install:
	uv sync
	uv run playwright install chromium

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
