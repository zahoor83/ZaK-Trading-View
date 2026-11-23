"""Asynchronous Alembic environment."""

from __future__ import annotations

import re
import asyncio
import logging.config

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.database.base import Base
import app.models  # noqa: F401
from app.database.config import MigrationDatabaseSettings

# Import future model modules here so Alembic can discover their metadata.
target_metadata = Base.metadata

config = context.config

def migration_version_table() -> str:
    """Return a strictly validated Alembic version-table identifier."""
    value = config.get_main_option(
        "version_table",
        "alembic_version",
    ).strip()

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise RuntimeError("Invalid Alembic version_table identifier.")

    return value

if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name)


def database_url() -> str:
    settings = MigrationDatabaseSettings.from_environment()
    if settings is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is required for database migrations.")

    # Alembic ConfigParser treats percent signs as interpolation markers.
    return settings.url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        version_table=migration_version_table(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        version_table=migration_version_table(),
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()