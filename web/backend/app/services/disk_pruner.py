"""Background disk pruner: drops aged-out report dirs and checkpoint DBs.

Runs on a 6-hour loop (see ``app/lifespan_hooks/disk_pruner.py``).

Layout (all under ``settings.data_dir``)::

    data_dir/
        logs/<TICKER>/<run_id>/...        -> per-run agent traces
        reports/<TICKER>_<DATE>/...       -> rendered report artefacts
        cache/<TICKER>-<DATE>-checkpoint.sqlite
                                          -> LangGraph checkpoint per (ticker, date)

Retention policy:

- **Report dirs** (immediate children of ``logs/<TICKER>/`` and of
  ``reports/``) are deleted when their directory ``mtime`` is older than
  ``retention_days`` days. We use ``mtime`` rather than the corresponding
  ``runs.created_at`` because a report dir may exist without a DB row
  (manual artifact, debug run, etc.) — ``mtime`` is the universal signal.

- **Checkpoint SQLite files** under ``cache/`` are keyed by
  ``<TICKER>-<DATE>-checkpoint.sqlite``. We parse the filename, look the
  ``(ticker, analysis_date)`` pair up in the ``runs`` table, and delete
  the file when the matching row's ``created_at`` is older than
  ``retention_days``. We prefer filename parsing over loading every Run
  row into memory because most installations will have far more rows
  than checkpoint files.

**Orphan checkpoint policy**: a checkpoint file whose filename does NOT
match any row in ``runs`` is **kept**. Without a corresponding DB row we
have no created_at signal, and silently deleting an orphan would punish
operators who placed a checkpoint by hand (or who lost their DB and have
nothing else to rebuild from). Documented here and tested in
``tests/test_disk_pruner.py::test_prune_once_keeps_orphan_checkpoints``.

Safety:
- Every unlink/rmtree target is validated against ``data_dir`` via
  ``_safe_inside`` (uses ``Path.resolve()`` + ``is_relative_to``). Nothing
  outside ``data_dir`` can ever be touched even if a name was somehow
  crafted to escape.
- Per-file errors are logged and swallowed; we never let one bad file
  abort the whole pass.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

log = logging.getLogger(__name__)


# Filenames look like "SPY-2026-05-19-checkpoint.sqlite". The DATE block is
# exactly the ISO ``YYYY-MM-DD`` shape. The ticker chunk is greedy but
# restricted to the chars ``safe_ticker_component`` allows (alnum + . _ - ^).
_CHECKPOINT_RE = re.compile(
    r"^(?P<ticker>[A-Za-z0-9._\^]+)-(?P<date>\d{4}-\d{2}-\d{2})-checkpoint\.sqlite$"
)


# --------------------------------------------------------------------------- #
# Safety helpers                                                              #
# --------------------------------------------------------------------------- #


def _safe_inside(path: Path, root: Path) -> bool:
    """True iff ``path`` resolves inside ``root``.

    Both sides are ``resolve()``d so symlinks, ``..`` segments, and Windows
    short-name aliases all flatten to a canonical absolute path. The
    standard-library ``is_relative_to`` is the canonical comparison.
    """
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        # Resolution failure (broken symlink, drive-letter mismatch on
        # Windows, etc.) -> safest to refuse.
        return False


# --------------------------------------------------------------------------- #
# Report-dir pruning                                                          #
# --------------------------------------------------------------------------- #


def _report_dir_candidates(data_dir: Path) -> Iterable[Path]:
    """Yield the per-run directories that are eligible for mtime pruning.

    - Under ``logs/`` we expect ``logs/<TICKER>/<run>/`` — the leaf, not
      the per-ticker bucket. Pruning the bucket would torch every run for
      that ticker in one swipe.
    - Under ``reports/`` we expect ``reports/<run-or-bundle>/`` directly.
    """
    logs_root = data_dir / "logs"
    if logs_root.is_dir():
        for ticker_dir in logs_root.iterdir():
            if not ticker_dir.is_dir():
                continue
            for run_dir in ticker_dir.iterdir():
                if run_dir.is_dir():
                    yield run_dir

    reports_root = data_dir / "reports"
    if reports_root.is_dir():
        for run_dir in reports_root.iterdir():
            if run_dir.is_dir():
                yield run_dir


def _prune_report_dirs(data_dir: Path, retention_days: int) -> int:
    """Delete each report dir whose mtime is older than retention. Returns count."""
    cutoff = time.time() - retention_days * 86400.0
    deleted = 0
    for candidate in _report_dir_candidates(data_dir):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            log.exception("disk_pruner.stat_failed", extra={"path": str(candidate)})
            continue
        if mtime >= cutoff:
            continue
        if not _safe_inside(candidate, data_dir):
            log.warning(
                "disk_pruner.refuse_outside_data_dir",
                extra={"path": str(candidate), "data_dir": str(data_dir)},
            )
            continue
        try:
            shutil.rmtree(candidate)
            deleted += 1
            log.info(
                "disk_pruner.report_deleted",
                extra={"path": str(candidate), "age_seconds": time.time() - mtime},
            )
        except Exception:
            log.exception("disk_pruner.report_delete_failed", extra={"path": str(candidate)})
    return deleted


# --------------------------------------------------------------------------- #
# Checkpoint pruning                                                          #
# --------------------------------------------------------------------------- #


async def _prune_checkpoints(data_dir: Path, retention_days: int, db) -> int:
    """Delete cache/*-checkpoint.sqlite files older than retention via runs.created_at."""
    from app.models import Run  # local import to avoid pulling ORM at module load

    cache_dir = data_dir / "cache"
    if not cache_dir.is_dir():
        return 0

    cutoff_dt = datetime.now(tz=timezone.utc) - _timedelta_days(retention_days)
    deleted = 0
    for candidate in cache_dir.iterdir():
        if not candidate.is_file():
            continue
        m = _CHECKPOINT_RE.match(candidate.name)
        if not m:
            # Unknown filename shape -> not ours, leave it alone.
            continue
        ticker = m.group("ticker")
        date_str = m.group("date")
        try:
            analysis_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            log.warning(
                "disk_pruner.bad_checkpoint_filename",
                extra={"path": str(candidate)},
            )
            continue

        try:
            result = await db.execute(
                select(Run.created_at)
                .where(Run.ticker == ticker)
                .where(Run.analysis_date == analysis_date)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            created_at = result.scalar_one_or_none()
        except Exception:
            log.exception(
                "disk_pruner.runs_lookup_failed",
                extra={"path": str(candidate), "ticker": ticker, "date": date_str},
            )
            continue

        if created_at is None:
            # Orphan: no signal to delete. See module docstring.
            continue
        # SQLite returns naive datetimes; normalize to UTC for the comparison.
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at >= cutoff_dt:
            continue
        if not _safe_inside(candidate, data_dir):
            log.warning(
                "disk_pruner.refuse_outside_data_dir",
                extra={"path": str(candidate), "data_dir": str(data_dir)},
            )
            continue
        try:
            candidate.unlink()
            deleted += 1
            log.info(
                "disk_pruner.checkpoint_deleted",
                extra={"path": str(candidate), "ticker": ticker, "date": date_str},
            )
        except Exception:
            log.exception(
                "disk_pruner.checkpoint_delete_failed",
                extra={"path": str(candidate)},
            )
    return deleted


def _timedelta_days(days: int):
    """Tiny helper — kept local so the import section stays tidy."""
    from datetime import timedelta

    return timedelta(days=days)


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


async def prune_once(data_dir: Path, retention_days: int, db) -> dict[str, int]:
    """Run a single prune pass.

    Returns counts ``{'reports_deleted', 'checkpoints_deleted'}``. Errors on
    individual files are logged + swallowed; the pass never bubbles them
    up so the 6-hour loop survives transient OS issues.
    """
    data_dir = Path(data_dir)
    if not data_dir.exists():
        log.info("disk_pruner.data_dir_missing", extra={"path": str(data_dir)})
        return {"reports_deleted": 0, "checkpoints_deleted": 0}

    reports_deleted = _prune_report_dirs(data_dir, retention_days)
    checkpoints_deleted = await _prune_checkpoints(data_dir, retention_days, db)

    counts = {
        "reports_deleted": reports_deleted,
        "checkpoints_deleted": checkpoints_deleted,
    }
    log.info("disk_pruner.pass_complete", extra=counts)
    return counts


async def prune_loop(
    data_dir: Path,
    retention_days: int,
    interval_seconds: int = 6 * 3600,
) -> None:
    """Forever loop: sleep, prune, repeat. Cancellable via ``asyncio.CancelledError``.

    A fresh DB session is opened per tick so we don't hold one open across
    a six-hour sleep. Exceptions inside a tick are logged and swallowed so
    a single transient failure doesn't take the loop down.
    """
    # Local import to keep module-load lean and avoid a circular import via
    # ``app.db`` if/when other lifespan hooks import this module first.
    from app.db import get_session_factory

    while True:
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
        try:
            factory = get_session_factory()
            async with factory() as session:
                await prune_once(data_dir, retention_days, session)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("disk_pruner.tick_failed")


__all__ = ["prune_once", "prune_loop"]
