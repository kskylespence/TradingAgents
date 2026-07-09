"""Add users table and run ownership.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed bootstrap admin id — lifespan hook upserts this row from env.
BOOTSTRAP_ADMIN_ID = "00000000-0000-0000-0000-000000000001"


def _uuid():
    return sa.String(length=36).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("username", name="users_username_key"),
    )

    op.add_column("runs", sa.Column("user_id", _uuid(), nullable=True))
    op.create_foreign_key(
        "runs_user_id_fkey",
        "runs",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Placeholder hash — bootstrap hook overwrites on startup.
    op.execute(
        sa.text(
            "INSERT INTO users (id, username, password_hash, role) "
            "VALUES (:id, 'bootstrap-admin', :hash, 'admin')"
        ).bindparams(
            id=BOOTSTRAP_ADMIN_ID,
            hash="$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderp",
        )
    )

    op.execute(
        sa.text("UPDATE runs SET user_id = :uid").bindparams(uid=BOOTSTRAP_ADMIN_ID)
    )

    op.alter_column("runs", "user_id", nullable=False)

    op.create_index(
        "runs_user_created_idx",
        "runs",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("runs_user_created_idx", table_name="runs")
    op.drop_constraint("runs_user_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "user_id")
    op.drop_table("users")
