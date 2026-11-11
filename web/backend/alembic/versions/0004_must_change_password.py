"""Add users.must_change_password (temporary passwords).

An admin reset (``PATCH /api/users/{id}/password``) sets the flag; the
user must choose a new password at next login before using anything else.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server default fills every existing row with false, so the
    # column can be NOT NULL from the start. Batch mode keeps SQLite
    # (local dev, e2e) on the same path as Postgres, as in 0003.
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("must_change_password")
