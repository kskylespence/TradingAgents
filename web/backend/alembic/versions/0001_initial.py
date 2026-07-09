"""Initial schema: runs, run_events, api_keys, user_defaults, login_attempts.

Mirrors the plan's "Database schema (Alembic 0001)" section verbatim.

Revision ID: 0001
Revises:
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --------------------------------------------------------------------------- #
# Dialect-portable type helpers                                               #
# --------------------------------------------------------------------------- #
#
# Production runs on Postgres; tests / dev may run on SQLite. JSONB, UUID,
# and INET are Postgres-only — we use `with_variant(...)` so the same
# migration succeeds on both.

def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _uuid():
    return sa.String(length=36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


def _inet():
    return sa.String(length=45).with_variant(postgresql.INET(), "postgresql")


def upgrade() -> None:
    # ---- runs ----
    op.create_table(
        "runs",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("asset_type", sa.String(length=16), nullable=False),
        sa.Column("analysis_date", sa.Date(), nullable=False),
        sa.Column("analysts", _json(), nullable=False),
        sa.Column("research_depth", sa.SmallInteger(), nullable=False),
        sa.Column("llm_provider", sa.String(length=32), nullable=False),
        sa.Column("quick_think_llm", sa.String(length=128), nullable=False),
        sa.Column("deep_think_llm", sa.String(length=128), nullable=False),
        sa.Column("thinking_config", _json(), nullable=True),
        sa.Column(
            "output_language",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'English'"),
        ),
        sa.Column(
            "checkpoint_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=True),
        sa.Column("decision_full", sa.Text(), nullable=True),
        sa.Column("report_dir", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("stats", _json(), nullable=True),
    )
    op.create_index("runs_created_at_idx", "runs", [sa.text("created_at DESC")])
    op.create_index(
        "runs_ticker_idx",
        "runs",
        ["ticker", sa.text("created_at DESC")],
    )
    # Partial index — Postgres only. Other dialects get a plain index on status.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "runs_status_idx",
            "runs",
            ["status"],
            postgresql_where=sa.text("status = 'running'"),
        )
    else:
        op.create_index("runs_status_idx", "runs", ["status"])

    # ---- run_events ----
    op.create_table(
        "run_events",
        sa.Column("run_id", _uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("type", sa.String(length=48), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], ondelete="CASCADE", name="run_events_run_id_fkey"
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name="run_events_pkey"),
    )

    # ---- api_keys ----
    op.create_table(
        "api_keys",
        sa.Column("provider_env", sa.String(length=64), primary_key=True),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---- user_defaults ----
    op.create_table(
        "user_defaults",
        sa.Column(
            "id",
            sa.SmallInteger(),
            primary_key=True,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("llm_provider", sa.String(length=32), nullable=True),
        sa.Column("quick_think_llm", sa.String(length=128), nullable=True),
        sa.Column("deep_think_llm", sa.String(length=128), nullable=True),
        sa.Column("research_depth", sa.SmallInteger(), nullable=True),
        sa.Column("analysts", _json(), nullable=True),
        sa.Column("output_language", sa.String(length=32), nullable=True),
        sa.Column("thinking_config", _json(), nullable=True),
        sa.Column(
            "enable_checkpoint",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="user_defaults_singleton"),
    )

    # ---- login_attempts ----
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


def downgrade() -> None:
    op.drop_index("login_attempts_ip_idx", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_table("user_defaults")
    op.drop_table("api_keys")
    op.drop_table("run_events")
    op.drop_index("runs_status_idx", table_name="runs")
    op.drop_index("runs_ticker_idx", table_name="runs")
    op.drop_index("runs_created_at_idx", table_name="runs")
    op.drop_table("runs")
