"""Migrations run on SQLite, not only Postgres.

Local dev and the Playwright e2e stack build their database with
``alembic upgrade head`` on SQLite. Migration 0003 altered ``runs`` with
plain ``op.create_foreign_key`` / ``op.alter_column``, which SQLite cannot
do ("No support for ALTER of constraints"), so no SQLite database could be
built past 0002 and the e2e suite never booted. Batch mode rebuilds the
table on SQLite and issues the same ALTERs on Postgres.

Runs the real ``alembic`` command in a subprocess so the app's cached
settings in this process are untouched.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.unit
def test_upgrade_head_and_downgrade_base_on_sqlite(tmp_path):
    db = tmp_path / "migrate.db"

    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]

    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "runs"} <= tables
        user_id = {r[1]: r for r in conn.execute("PRAGMA table_info(runs)")}["user_id"]
        assert user_id[3] == 1, "runs.user_id must be NOT NULL"
        fks = [(r[2], r[3], r[4]) for r in conn.execute("PRAGMA foreign_key_list(runs)")]
        assert ("users", "user_id", "id") in fks
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(runs)")}
        assert "runs_user_created_idx" in indexes
        admins = conn.execute("SELECT username, role FROM users").fetchall()
        assert admins == [("bootstrap-admin", "admin")]
    finally:
        conn.close()

    down = _alembic(db, "downgrade", "base")
    assert down.returncode == 0, down.stderr[-2000:]
