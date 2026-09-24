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

    # Batch mode: SQLite cannot ALTER constraints, so on SQLite alembic
    # rebuilds the table; on Postgres these are the same plain ALTERs as
    # before (databases that already ran 0003 are unaffected).
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("user_id", _uuid(), nullable=True))
        batch_op.create_foreign_key(
            "runs_user_id_fkey",
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # Placeholder hash — bootstrap hook overwrites on startup.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "INSERT INTO users (id, username, password_hash, role) "
                "VALUES (CAST(:id AS uuid), 'bootstrap-admin', :hash, 'admin')"
            ).bindparams(
                id=BOOTSTRAP_ADMIN_ID,
                hash="$2b$12$placeholderplaceholderplaceholderplaceholderplaceholderp",
            )
        )
        op.execute(
            sa.text("UPDATE runs SET user_id = CAST(:uid AS uuid)").bindparams(
                uid=BOOTSTRAP_ADMIN_ID
            )
        )
    else:
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

    with op.batch_alter_table("runs") as batch_op:
        batch_op.alter_column("user_id", existing_type=_uuid(), nullable=False)

    op.create_index(
        "runs_user_created_idx",
        "runs",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("runs_user_created_idx", table_name="runs")
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("runs_user_id_fkey", type_="foreignkey")
        batch_op.drop_column("user_id")
    op.drop_table("users")
