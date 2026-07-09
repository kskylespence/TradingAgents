"""Tests for the disk pruner service and its lifespan hook.

Covers:
- ``prune_once`` deletes only the report directories whose mtime is older
  than ``retention_days``.
- Per-``(ticker, date)`` checkpoint SQLite files are deleted when their
  matching ``runs`` row's ``created_at`` is older than retention; rows
  inside retention keep their checkpoint file.
- **Orphan-checkpoint policy**: a checkpoint file whose filename does not
  match any row in ``runs`` is **kept** (no signal to delete). This is the
  safer default — see the module docstring of ``app.services.disk_pruner``.
- Path-safety: a sibling file *outside* ``data_dir`` is never unlinked,
  even if a (hypothetical) malicious / corrupted name resolves into it.
- The lifespan hook starts a task on ``startup`` and cancels it cleanly
  on ``shutdown`` with no asyncio warnings.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import TEST_ADMIN_ID

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


def _age_seconds(days: int) -> float:
    return days * 86400.0


def _set_mtime(p: Path, days_old: int) -> None:
    """Stamp ``p``'s mtime/atime to ``days_old`` days in the past."""
    when = time.time() - _age_seconds(days_old)
    os.utime(p, (when, when))


@pytest.fixture
async def db_setup(tmp_path):
    """File-based SQLite engine with all tables created."""
    from app import models  # noqa: F401 — register tables on Base.metadata
    from app.db import Base

    db_path = tmp_path / "pruner.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


def _seed_layout(
    data_dir: Path,
    *,
    old_report_days: int = 100,
    new_report_days: int = 5,
) -> dict[str, Path]:
    """Create the on-disk layout described in the disk_pruner brief.

    Returns a dict of named paths so individual tests can assert against them.
    """
    logs = data_dir / "logs"
    reports = data_dir / "reports"
    cache = data_dir / "cache"
    logs.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    # An OLD per-run report dir (under logs/) — should be deleted.
    old_log = logs / "SPY" / "old_run"
    old_log.mkdir(parents=True)
    (old_log / "market_report.md").write_text("# market", encoding="utf-8")
    _set_mtime(old_log / "market_report.md", old_report_days)
    _set_mtime(old_log, old_report_days)

    # A NEW per-run report dir — should be kept.
    new_log = logs / "SPY" / "new_run"
    new_log.mkdir(parents=True)
    (new_log / "market_report.md").write_text("# market", encoding="utf-8")
    _set_mtime(new_log / "market_report.md", new_report_days)
    _set_mtime(new_log, new_report_days)

    # An OLD report dir under reports/ — should be deleted (mirrors logs/).
    old_report = reports / "SPY_2025-01-01"
    old_report.mkdir(parents=True)
    (old_report / "final_trade_decision.md").write_text("Buy", encoding="utf-8")
    _set_mtime(old_report / "final_trade_decision.md", old_report_days)
    _set_mtime(old_report, old_report_days)

    # A NEW report dir under reports/ — should be kept.
    new_report = reports / "SPY_2026-05-15"
    new_report.mkdir(parents=True)
    (new_report / "final_trade_decision.md").write_text("Hold", encoding="utf-8")
    _set_mtime(new_report / "final_trade_decision.md", new_report_days)
    _set_mtime(new_report, new_report_days)

    # Orphan checkpoint (no runs row matches this filename) — kept.
    orphan_checkpoint = cache / "SPY-2026-01-01-checkpoint.sqlite"
    orphan_checkpoint.write_bytes(b"sqlite-pretend")

    # Recent-run checkpoint (corresponding row 5 days old) — kept.
    recent_checkpoint = cache / "SPY-2026-05-15-checkpoint.sqlite"
    recent_checkpoint.write_bytes(b"sqlite-pretend")

    # Old-run checkpoint (corresponding row 100 days old) — should be deleted.
    old_run_checkpoint = cache / "AAPL-2025-12-01-checkpoint.sqlite"
    old_run_checkpoint.write_bytes(b"sqlite-pretend")

    return {
        "old_log": old_log,
        "new_log": new_log,
        "old_report": old_report,
        "new_report": new_report,
        "orphan_checkpoint": orphan_checkpoint,
        "recent_checkpoint": recent_checkpoint,
        "old_run_checkpoint": old_run_checkpoint,
    }


async def _seed_runs(factory, *, recent_days: int, old_days: int) -> None:
    """Insert two runs: one recent (SPY 2026-05-15), one old (AAPL 2025-12-01)."""
    from app.models import Run

    now = datetime.now(tz=timezone.utc)
    async with factory() as session:
        session.add(
            Run(
                id=str(uuid.uuid4()),
                user_id=TEST_ADMIN_ID,
                ticker="SPY",
                asset_type="stock",
                analysis_date=date(2026, 5, 15),
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_think_llm="gpt-4o-mini",
                deep_think_llm="gpt-4o",
                output_language="English",
                checkpoint_enabled=True,
                status="completed",
                created_at=now - timedelta(days=recent_days),
            )
        )
        session.add(
            Run(
                id=str(uuid.uuid4()),
                user_id=TEST_ADMIN_ID,
                ticker="AAPL",
                asset_type="stock",
                analysis_date=date(2025, 12, 1),
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_think_llm="gpt-4o-mini",
                deep_think_llm="gpt-4o",
                output_language="English",
                checkpoint_enabled=True,
                status="completed",
                created_at=now - timedelta(days=old_days),
            )
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# prune_once                                                                  #
# --------------------------------------------------------------------------- #


async def test_prune_once_deletes_old_report_dirs_and_keeps_new(
    tmp_path, db_setup
) -> None:
    """Old report dirs vanish, new ones survive, counts are reported."""
    from app.services.disk_pruner import prune_once

    _engine, factory = db_setup
    paths = _seed_layout(tmp_path)
    await _seed_runs(factory, recent_days=5, old_days=100)

    async with factory() as session:
        counts = await prune_once(tmp_path, retention_days=90, db=session)

    assert not paths["old_log"].exists(), "old logs/<ticker>/<run> must be deleted"
    assert paths["new_log"].exists(), "new logs/<ticker>/<run> must be kept"
    assert not paths["old_report"].exists(), "old reports/<run> must be deleted"
    assert paths["new_report"].exists(), "new reports/<run> must be kept"

    assert counts["reports_deleted"] == 2
    # Old-run checkpoint should also have been deleted, but orphan + recent stay.
    assert counts["checkpoints_deleted"] == 1


async def test_prune_once_keeps_orphan_checkpoints(tmp_path, db_setup) -> None:
    """A checkpoint file with no matching Run row is preserved (orphan policy)."""
    from app.services.disk_pruner import prune_once

    _engine, factory = db_setup
    paths = _seed_layout(tmp_path)
    await _seed_runs(factory, recent_days=5, old_days=100)

    async with factory() as session:
        await prune_once(tmp_path, retention_days=90, db=session)

    assert paths["orphan_checkpoint"].exists(), (
        "orphan checkpoint (no Run row) must be preserved per the documented policy"
    )
    assert paths["recent_checkpoint"].exists(), (
        "checkpoint for an in-retention run must be preserved"
    )
    assert not paths["old_run_checkpoint"].exists(), (
        "checkpoint for an out-of-retention run must be deleted"
    )


async def test_prune_once_swallows_unlink_errors(
    tmp_path, db_setup, monkeypatch
) -> None:
    """A failure on one file must not abort the whole pass."""
    import app.services.disk_pruner as pruner

    _engine, factory = db_setup
    paths = _seed_layout(tmp_path)
    await _seed_runs(factory, recent_days=5, old_days=100)

    real_rmtree = pruner.shutil.rmtree
    seen: list[Path] = []

    def boom_rmtree(p, *a, **kw):
        seen.append(Path(p))
        if Path(p) == paths["old_log"]:
            raise PermissionError("simulated lock")
        return real_rmtree(p, *a, **kw)

    monkeypatch.setattr(pruner.shutil, "rmtree", boom_rmtree)

    async with factory() as session:
        counts = await pruner.prune_once(tmp_path, retention_days=90, db=session)

    # The other old dir still got deleted; the failing one is just skipped.
    assert not paths["old_report"].exists()
    assert counts["reports_deleted"] == 1


async def test_prune_once_never_deletes_outside_data_dir(
    tmp_path, db_setup
) -> None:
    """Hand-crafted absolute paths that escape ``data_dir`` are refused."""
    from app.services.disk_pruner import _safe_inside

    outside = tmp_path.parent / "sibling_outside_data_dir"
    outside.mkdir()
    sentinel = outside / "do_not_touch.txt"
    sentinel.write_text("keep me", encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Direct safety check.
    assert _safe_inside(sentinel, data_dir) is False
    assert _safe_inside(data_dir / "logs" / "x", data_dir) is True

    # And after a full prune pass against a real DB session, the sentinel
    # is untouched (sanity — there's no signal to delete it anyway).
    from app.services.disk_pruner import prune_once

    _engine, factory = db_setup
    async with factory() as session:
        await prune_once(data_dir, retention_days=90, db=session)
    assert sentinel.exists()


# --------------------------------------------------------------------------- #
# Lifespan hook                                                               #
# --------------------------------------------------------------------------- #


async def test_lifespan_hook_starts_and_stops_cleanly(monkeypatch) -> None:
    """``start(app)`` creates a task; ``stop(app)`` cancels it without warnings."""
    import app.lifespan_hooks.disk_pruner as hook
    import app.services.disk_pruner as svc
    from fastapi import FastAPI

    # Patch the loop to a fast no-op so the test doesn't sleep for 6 hours.
    async def fake_loop(data_dir, retention_days, interval_seconds: int = 1):
        # Long sleep so the task is alive when stop() cancels it.
        await asyncio.sleep(3600)

    monkeypatch.setattr(svc, "prune_loop", fake_loop)

    app_obj = FastAPI()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any RuntimeWarning fails the test
        await hook.start(app_obj)
        task = hook._task
        assert task is not None
        assert not task.done()
        await hook.stop(app_obj)
        assert task.cancelled() or task.done()
