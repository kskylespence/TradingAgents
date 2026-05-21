"""SQLAlchemy 2.0 declarative ORM models.

Mirrors the plan's "Database schema (Alembic 0001)" section:
- runs, run_events, api_keys, user_defaults, login_attempts.

Column types use `JSON` (SQLAlchemy generic) which the Postgres dialect
compiles to JSONB via a `with_variant`. INET and UUID likewise have
SQLite-friendly fallbacks so unit tests can run on aiosqlite.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .db import Base


# JSONB on Postgres, JSON elsewhere (SQLite for tests).
JsonType = JSON().with_variant(JSONB(), "postgresql")
# Native UUID on Postgres, String(36) elsewhere.
UuidType = String(36).with_variant(UUID(as_uuid=True), "postgresql")
# INET on Postgres, String(45) elsewhere (enough for IPv6).
InetType = String(45).with_variant(INET(), "postgresql")


class Run(Base):
    """One row per analysis run."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UuidType, primary_key=True, default=uuid.uuid4)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_date: Mapped[date] = mapped_column(Date, nullable=False)
    analysts: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    research_depth: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    llm_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    quick_think_llm: Mapped[str] = mapped_column(String(128), nullable=False)
    deep_think_llm: Mapped[str] = mapped_column(String(128), nullable=False)
    thinking_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JsonType, nullable=True)
    output_language: Mapped[str] = mapped_column(
        String(32), nullable=False, default="English"
    )
    checkpoint_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    rating: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    decision_full: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_dir: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    stats: Mapped[Optional[dict[str, Any]]] = mapped_column(JsonType, nullable=True)

    events: Mapped[list["RunEvent"]] = relationship(
        "RunEvent", back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("runs_created_at_idx", created_at.desc()),
        Index("runs_ticker_idx", "ticker", created_at.desc()),
        # Partial index on Postgres; we attach a regular index on SQLite for tests.
        Index(
            "runs_status_idx",
            "status",
            postgresql_where=status == "running",
        ),
    )


class RunEvent(Base):
    """Append-only event log for SSE replay."""

    __tablename__ = "run_events"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UuidType,
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)

    run: Mapped[Run] = relationship("Run", back_populates="events")

    __table_args__ = (PrimaryKeyConstraint("run_id", "seq", name="run_events_pkey"),)


class ApiKey(Base):
    """Per-provider Fernet-encrypted API keys."""

    __tablename__ = "api_keys"

    provider_env: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserDefaults(Base):
    """Form pre-fill values (single-row table, id always = 1)."""

    __tablename__ = "user_defaults"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    llm_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    quick_think_llm: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    deep_think_llm: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    research_depth: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    analysts: Mapped[Optional[list[str]]] = mapped_column(JsonType, nullable=True)
    output_language: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    thinking_config: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JsonType, nullable=True
    )
    enable_checkpoint: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (CheckConstraint("id = 1", name="user_defaults_singleton"),)


class LoginAttempt(Base):
    """In-memory rate limiter backup; persists for ban-after-restart."""

    __tablename__ = "login_attempts"

    # Surrogate PK required by the ORM so SQLAlchemy can emit
    # INSERT ... RETURNING id on each rate-limit record. The column is
    # added by Alembic revision 0002 (the original 0001 plan omitted it,
    # which crashed every login on Postgres until we caught it in
    # production — tests use Base.metadata.create_all and never exercised
    # the migration path).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(InetType, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        Index("login_attempts_ip_idx", "ip", attempted_at.desc()),
    )
