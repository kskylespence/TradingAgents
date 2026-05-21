"""Health-check router.

`GET /api/health` — public liveness + lightweight readiness probe.

Returns::

    {
        "status": "ok" | "degraded",
        "db":     "ok" | "down",
        "disk_free_mb": int | None,
        "active_run_id": str | None
    }

Design notes
------------
* **No auth.** Coolify (and any other reverse-proxy / orchestrator probe)
  hits this anonymously. Adding a dependency that requires the JWT here
  would silently break deploy health checks.
* **Always HTTP 200** when the handler executes — even when the DB is
  down. Coolify treats any non-2xx as "container unhealthy" and will
  restart it; that's the wrong reaction to a transient DB blip. Instead
  we signal degradation in the body (`status: "degraded"`, `db: "down"`)
  so dashboards / humans can see it while the container itself stays up.
  (If a probe ever needs the strict 503 behavior we can layer a second
  path like `/health/strict` later; the plan does not call for it today.)
* **`active_run_id` is lazily imported** from `app.services.run_service`,
  which is built in a later wave. Until that module exists we return
  `None` rather than failing — the health endpoint must work from day 1.

This is the single source of truth for `/api/health`. The earlier
placeholder lived in `app/main.py`; it was removed once this router
landed because FastAPI's first-match-wins routing meant the placeholder
otherwise shadowed this real handler — Coolify would have reported
"ok" even on a DB-down failure. A separate `/api/_bootstrap_health`
remains in `main.py` purely as a router-import-failure fallback.
"""

from __future__ import annotations

import logging
import shutil
from typing import Optional

from fastapi import APIRouter
from sqlalchemy import text

from ..config import get_settings
from ..db import get_engine
from . import register

log = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


async def _check_db() -> None:
    """Run a trivial `SELECT 1` against the async engine.

    Raises whatever the engine raises on failure. Broken out as a module-
    level function so tests can monkeypatch it to simulate DB-down.
    """
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


def _disk_free_mb() -> Optional[int]:
    """Free space on `settings.data_dir` in whole MB, or None if missing."""
    data_dir = get_settings().data_dir
    if not data_dir.exists():
        log.warning(
            "health.data_dir_missing",
            extra={"data_dir": str(data_dir)},
        )
        return None
    usage = shutil.disk_usage(str(data_dir))
    return int(usage.free // (1024 * 1024))


def _active_run_id() -> Optional[str]:
    """Best-effort lookup of the currently-active run id.

    `app.services.run_service` lands in a later wave. Until then we
    swallow `ImportError` and return None so the endpoint stays useful.
    Any other exception (e.g. the service exists but throws) is also
    swallowed and logged — health checks must not crash on internal
    errors elsewhere.
    """
    try:
        from ..services import run_service  # type: ignore[attr-defined]
    except ImportError:
        return None
    try:
        getter = getattr(run_service, "get_active_run_id", None)
        if getter is None:
            return None
        value = getter()
        return str(value) if value is not None else None
    except Exception:  # pragma: no cover — defensive, health must not crash
        log.exception("health.active_run_id_lookup_failed")
        return None


@router.get("", summary="Liveness + lightweight readiness probe")
async def health() -> dict:
    """Public health endpoint. See module docstring for full contract."""
    db_status = "ok"
    overall = "ok"
    try:
        await _check_db()
    except Exception:
        log.exception("health.db_check_failed")
        db_status = "down"
        overall = "degraded"

    return {
        "status": overall,
        "db": db_status,
        "disk_free_mb": _disk_free_mb(),
        "active_run_id": _active_run_id(),
    }


register(router)
