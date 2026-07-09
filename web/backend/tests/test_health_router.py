"""Tests for the real /api/health router.

Contract (per the plan's API table row for `GET /api/health`):

    {
        "status": "ok" | "degraded",
        "db": "ok" | "down",
        "disk_free_mb": int | None,
        "active_run_id": str | None,
    }

Behaviors verified:

* PUBLIC — no auth header required (Coolify probes hit this anonymously).
* DB up  -> status=ok,        db=ok
* DB down -> status=degraded, db=down  (we deliberately stay 200 so Coolify
  still considers the container "running"; degradation is signalled in the
  body. The plan does not call for 503 — see the design rationale comment
  in `app/routers/health.py`.)
* disk_free_mb is an int when settings.data_dir exists; None otherwise.
* active_run_id is None when run_service is not yet wired (Wave 3).

The placeholder in `app/main.py` registers `GET /api/health` BEFORE
`app.include_router(...)` runs, and FastAPI matches in registration order
(first match wins). The router under test therefore lives at the same
path `/health` but the placeholder still wins for `GET /api/health` until
the placeholder is removed. To test this router directly we mount it on a
fresh FastAPI app — that matches how it would behave once the placeholder
is gone (the placeholder is explicitly temporary per its docstring).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_only_health_router() -> FastAPI:
    """Build a FastAPI app that mounts only the health router under /api.

    This isolates the router under test from the placeholder route that
    `app.main.create_app` installs at `/api/health`. The placeholder is
    flagged in its own docstring as temporary; once it's removed the real
    router (registered via the auto-discovery registry) will own that path.
    """
    from app.routers.health import router as health_router

    app = FastAPI()
    app.include_router(health_router, prefix="/api")
    return app


def test_health_router_is_registered_in_routers_registry() -> None:
    """Importing the routers package must auto-discover health.py.

    The autoload runs once at package import time and registers each
    submodule's router. We assert the health router instance ended up in
    `ROUTERS` rather than reloading the package (reload is a no-op for
    side-effects when submodules are already in `sys.modules`).
    """
    from app.routers import ROUTERS
    from app.routers.health import router as health_router

    assert health_router in ROUTERS, (
        "health router not auto-discovered into ROUTERS registry"
    )


def test_health_ok_when_db_up_and_data_dir_exists(tmp_path, monkeypatch) -> None:
    """Happy path: DB SELECT 1 succeeds, data_dir exists -> status=ok."""
    from app.config import get_settings

    # Point data_dir at a real (empty) tmp dir so shutil.disk_usage works.
    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()

    # Required fields present.
    for key in ("status", "db", "disk_free_mb", "active_run_id"):
        assert key in body, f"missing key {key!r} in body {body!r}"

    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert isinstance(body["disk_free_mb"], int)
    assert body["disk_free_mb"] >= 0
    assert body["active_run_id"] is None  # run_service not yet wired

    get_settings.cache_clear()


def test_health_degraded_when_db_raises(tmp_path, monkeypatch) -> None:
    """DB SELECT 1 raises -> status=degraded, db=down, still 200."""
    from app.config import get_settings
    from app.routers import health as health_module

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    async def _boom() -> None:
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(health_module, "_check_db", _boom)

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    # We deliberately return 200 with a degraded body so Coolify keeps the
    # container "running" but the body advertises the degradation to humans
    # / dashboards. This is the explicit chosen behavior of the two options
    # in the task brief.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"

    get_settings.cache_clear()


def test_disk_free_mb_is_none_when_data_dir_missing(tmp_path, monkeypatch) -> None:
    """If settings.data_dir does not exist, disk_free_mb must be None."""
    from app.config import get_settings

    missing = tmp_path / "definitely-does-not-exist"
    assert not missing.exists()

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(missing))

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["disk_free_mb"] is None

    get_settings.cache_clear()


def test_health_endpoint_is_public_no_auth_required(tmp_path, monkeypatch) -> None:
    """No Authorization header => still 200 (Coolify probes are anonymous)."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        # No headers, no cookies, no token.
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    # Sanity: an explicit empty Authorization header also still works.
    with TestClient(app) as client:
        resp = client.get("/api/health", headers={"Authorization": ""})
    assert resp.status_code == 200, resp.text

    get_settings.cache_clear()


def test_active_run_id_none_when_run_service_missing(tmp_path, monkeypatch) -> None:
    """run_service is built in Wave 3; until then active_run_id must be None."""
    import sys

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Ensure `app.services.run_service` isn't importable for this test.
    sys.modules.pop("app.services.run_service", None)

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["active_run_id"] is None

    get_settings.cache_clear()
