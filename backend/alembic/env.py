"""Alembic migration environment.

Bridges Alembic to the application's async engine and model metadata. The
database URL and target metadata are pulled from the app so migrations always
match runtime configuration. No tables are defined yet — autogenerate will
produce empty migrations until models are added to :mod:`app.models`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.core.config import get_settings
from app.database import Base

# Importing the models package registers any ORM classes on Base.metadata.
import app.models  # noqa: F401  (side-effect import for autogenerate)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata Alembic compares against the live database for autogeneration.
target_metadata = Base.metadata

# Resolve the database URL from application settings (single source of truth).
DATABASE_URL = get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure the context with a live connection and run migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the async engine."""
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
