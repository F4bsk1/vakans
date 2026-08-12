"""Alembic environment.

The database URL comes from `app.config.Settings`, not from alembic.ini, so there is exactly
one place that knows how to reach the database and the Supabase swap stays a single env var.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config import get_settings
from app.models import Base, alembic_include_object

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Allow tests (and anything else) to point a run at another database.
_url = context.get_x_argument(as_dictionary=True).get("db_url") or get_settings().alembic_url
config.set_main_option("sqlalchemy.url", _url)


def _configure(connection: Connection | None = None, **kw: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these two, a changed column type or server default silently fails to show up
        # in autogenerate, and the model/migration drift test loses most of its value.
        compare_type=True,
        compare_server_default=True,
        include_object=alembic_include_object,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
