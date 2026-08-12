"""Shared test fixtures.

Two things worth knowing before you write a test here:

1. The test database is created from Python via psycopg, not `psql` — psql is not installed
   on every dev machine and we do not want it to become a requirement.

2. `db_session` runs each test inside an outer transaction that is rolled back afterwards.
   That is fast and perfectly isolated, but the SQLAlchemy `after_commit` hook that publishes
   events to the bus never meaningfully fires under it, because the "commit" is only a
   savepoint release. Tests that assert an event reached the bus must use
   `committing_db_session` and be marked `@pytest.mark.db_commit`. Without this note people
   lose an hour to "my event never arrives in tests".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from app.config import Settings, get_settings
from app.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"


def _psycopg_url(url: str) -> str:
    """SQLAlchemy's `postgresql+psycopg://` -> the plain libpq URL psycopg wants."""
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(scope="session")
def settings() -> Settings:
    base = get_settings()
    return base.model_copy(
        update={
            "env": "test",
            "database_url": base.url_for_database(base.test_database_name),
            "llm_provider": "fake",
            "sms_provider": "fake",
            "founder_mobile": "+46701234567",
        }
    )


@pytest.fixture(scope="session")
def test_database(settings: Settings) -> Iterator[str]:
    """Drop and recreate the test database, then migrate it to head."""
    admin_url = _psycopg_url(get_settings().url_for_database("postgres"))
    name = settings.test_database_name

    try:
        conn = psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.exit(f"Cannot reach Postgres ({exc}). Run `make db-up`.", returncode=1)

    with conn, conn.cursor() as cur:
        cur.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = %s and pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(f'drop database if exists "{name}"')
        cur.execute(f'create database "{name}"')

    # Run the real migrations rather than metadata.create_all: this validates the migration
    # path on every test run, which is where model/migration drift actually gets caught.
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    cfg.cmd_opts = type("O", (), {"x": [f"db_url={settings.database_url}"]})()  # type: ignore[assignment]
    command.upgrade(cfg, "head")

    yield settings.database_url


@pytest.fixture(scope="session")
async def engine(test_database: str) -> AsyncIterator[AsyncEngine]:
    # Session-scoped, so every DB fixture and test shares one event loop. That is why
    # pyproject pins both asyncio loop scopes to "session" — an engine created on one loop
    # cannot hand out connections on another.
    eng = create_async_engine(test_database)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as conn:
        yield conn


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Isolated per test: everything is rolled back, nothing is truncated."""
    trans = await db_connection.begin()
    maker = async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with maker() as session:
        yield session
    await trans.rollback()


@pytest.fixture
async def committing_db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Real commits, for tests that assert after-commit behaviour (bus publication, races).

    Truncates every table afterwards, so it is slower than `db_session`. Mark such tests with
    `@pytest.mark.db_commit`.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session

    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"truncate {tables} restart identity cascade"))
