"""Async SQLAlchemy 2.0 engine + session factory.

Provides:
- `Base` declarative base for ORM models (see app/models.py).
- `engine` lazily-constructed via `get_engine()`.
- `AsyncSessionLocal` session factory.
- `get_session()` FastAPI dependency yielding an `AsyncSession`.

Engine creation is wrapped in a small lazy accessor so tests can override
`DATABASE_URL` via env before the first call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        # echo=False by default; tests/devs flip via SQLALCHEMY_ECHO if desired.
        _engine = create_async_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, creating it on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an `AsyncSession`.

    Usage::

        from fastapi import Depends
        from app.db import get_session

        @router.get(...)
        async def handler(db: AsyncSession = Depends(get_session)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the engine and clear the cached factory. Used in lifespan shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None
