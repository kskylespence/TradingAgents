"""Alembic env — async-aware, driven by `app.config.Settings`.

Supports both 'online' (live DB) and 'offline' (SQL script) modes.
The async engine is constructed from `Settings.database_url`; in tests
this can be `sqlite+aiosqlite:///:memory:` (the default) or a Postgres
URL via env vars.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make `app.*` importable when alembic is run from web/backend/.
_HERE = Path(__file__).resolve().parent
_BACKEND_ROOT = _HERE.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: F401,E402  ensures models are registered on Base.metadata

# Alembic Config object, providing access to alembic.ini values.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the DB URL from Settings (which reads DATABASE_URL env).
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Target metadata for autogenerate.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    is_sqlite = connection.dialect.name == "sqlite"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # batch mode is needed for SQLite ALTER TABLE limitations.
        render_as_batch=is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Construct an AsyncEngine and run migrations within it."""
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = settings.database_url
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    # If the URL is sync (e.g. an operator points alembic at psycopg2),
    # fall back to a sync engine path. The typical case is async.
    url = config.get_main_option("sqlalchemy.url") or ""
    if "+asyncpg" in url or "+aiosqlite" in url:
        asyncio.run(run_async_migrations())
        return
    # Sync fallback (e.g. postgresql:// without +asyncpg).
    from sqlalchemy import create_engine

    engine = create_engine(url, poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        do_run_migrations(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
