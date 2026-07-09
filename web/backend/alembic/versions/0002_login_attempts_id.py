"""Add id primary key to login_attempts.

The 0001 migration omitted any primary key from ``login_attempts`` — the
table is queried by ``(ip, attempted_at)`` so a PK isn't load-bearing for
the rate limiter's hot path. But the ORM model in ``app/models.py`` does
declare ``id: Mapped[int] = mapped_column(Integer, primary_key=True,
autoincrement=True)``, so SQLAlchemy emits ``INSERT ... RETURNING id``
on every rate-limit record. On SQLite (used in tests via
``Base.metadata.create_all``) the ORM schema wins and tests pass. On
Postgres (used in production via ``alembic upgrade head``) the column
doesn't exist and every login attempt 500s with::

    asyncpg.exceptions.UndefinedColumnError:
    column login_attempts.id does not exist

This migration adds the missing column. The table has no business rows
to preserve (it's a transient rate-limit ledger; losing a few entries
just resets the buckets), so we drop and recreate to keep both
PostgreSQL and SQLite happy without a dialect branch.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inet():
    return sa.String(length=45).with_variant(postgresql.INET(), "postgresql")


def upgrade() -> None:
    op.drop_index("login_attempts_ip_idx", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ip", _inet(), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "login_attempts_ip_idx",
        "login_attempts",
        ["ip", sa.text("attempted_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("login_attempts_ip_idx", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.create_table(
        "login_attempts",
        sa.Column("ip", _inet(), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "login_attempts_ip_idx",
        "login_attempts",
        ["ip", sa.text("attempted_at DESC")],
    )
