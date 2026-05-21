"""/api/health exposes Ollama reachability when ollama is the active provider.

This lets operators see at a glance whether the upstream LLM is
reachable, without setting up a separate monitoring stack. The outer
`status` field MUST stay "ok" when Ollama is down — Coolify must not
restart the container for an upstream LLM blip (existing invariant
from `health.py`).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import install_fake_httpx_ollama as _install_fake_httpx


def _make_app_with_only_health_router() -> FastAPI:
    from app.routers.health import router as health_router

    app = FastAPI()
    app.include_router(health_router, prefix="/api")
    return app


def test_health_includes_ollama_block_when_provider_is_ollama(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"])

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "ollama" in body
    assert body["ollama"]["status"] == "ok"
    assert body["ollama"]["url"] == "https://ollama.com/v1"
    assert body["ollama"]["model_count"] == 2

    get_settings.cache_clear()


def test_health_outer_status_stays_ok_when_ollama_down(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coolify must NOT restart the container for an upstream LLM blip.

    Mirrors the DB-down behavior: degradation is signalled in the body
    (`ollama.status=down`) but the outer `status` stays `"ok"` so the
    container itself stays healthy.
    """
    import httpx

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    _install_fake_httpx(
        monkeypatch, raise_exc=httpx.ConnectError("upstream unreachable")
    )

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    # Critical: outer status NOT degraded when only ollama is down.
    assert body["status"] == "ok"
    assert body["ollama"]["status"] == "down"
    # The error field carries enough detail for ops triage.
    assert body["ollama"]["error"] is not None
    # On a genuine connection failure model_count is None (we have no
    # honest count to report — distinct from "ok with 0 models").
    assert body["ollama"]["model_count"] is None

    get_settings.cache_clear()


def test_health_no_ollama_block_when_provider_is_openai(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # When the active provider isn't ollama, no ollama block at all.
    assert body.get("ollama") is None

    get_settings.cache_clear()


def test_health_reports_ok_with_zero_models_when_upstream_succeeds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream HTTP 200 with empty `data` is honest success — status must be 'ok'.

    Reviewer flagged item #4: the old probe reported 'down' for this case,
    causing false alarms when an Ollama Cloud account had zero models
    provisioned (cold start / licensing). The fix tracks per-attempt
    status separately from the cache so we can distinguish "upstream said
    []" from "upstream was unreachable".
    """
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    _install_fake_httpx(monkeypatch, ids=[])  # success, but zero models

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ollama"]["status"] == "ok"
    assert body["ollama"]["model_count"] == 0
    assert body["ollama"]["error"] is None

    get_settings.cache_clear()


def test_health_response_matches_schema_contract(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape of the response must validate against `HealthResponse`.

    Reviewer flagged item #5: the endpoint previously returned an untyped
    `dict`. Adding `response_model=HealthResponse` means FastAPI now
    coerces and validates the response against the Pydantic model on the
    way out. This test pins that contract — extra fields would be dropped
    by Pydantic's default exclude policy.
    """
    from app.config import get_settings
    from app.schemas import HealthResponse

    get_settings.cache_clear()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b"])

    app = _make_app_with_only_health_router()
    with TestClient(app) as client:
        resp = client.get("/api/health")

    assert resp.status_code == 200
    # Round-trip via the schema — raises ValidationError if any field
    # is the wrong type or missing.
    HealthResponse.model_validate(resp.json())

    get_settings.cache_clear()


def test_ollama_health_schema_accepts_all_status_literals() -> None:
    """Pin every value of ``OllamaHealth.status`` against the schema.

    Reviewer item #2 (follow-up): the HTTP round-trip test above only
    exercises the ``"ok"`` path. If someone narrows the literal back to
    ``["ok", "down"]`` (dropping ``"unknown"``), the round-trip test
    would still pass because the live probe path doesn't naturally
    produce ``"unknown"`` in normal operation. These direct
    ``model_validate`` calls fail loudly the moment any variant is
    removed.
    """
    from app.schemas import OllamaHealth

    # "ok" — last fetch succeeded, model_count is the real count.
    OllamaHealth.model_validate(
        {
            "status": "ok",
            "url": "https://ollama.com/v1",
            "model_count": 38,
            "error": None,
        }
    )
    # "ok" with zero models — legitimate empty success (the fix for #4).
    OllamaHealth.model_validate(
        {
            "status": "ok",
            "url": "https://ollama.com/v1",
            "model_count": 0,
            "error": None,
        }
    )
    # "down" — last fetch failed; error carries the reason.
    OllamaHealth.model_validate(
        {
            "status": "down",
            "url": "https://ollama.com/v1",
            "model_count": None,
            "error": "ConnectError('upstream unreachable')",
        }
    )
    # "unknown" — never probed in this process.
    OllamaHealth.model_validate(
        {
            "status": "unknown",
            "url": "https://ollama.com/v1",
            "model_count": None,
            "error": None,
        }
    )

    # Negative case: any unknown literal must be rejected.
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        OllamaHealth.model_validate(
            {
                "status": "degraded",  # not a valid OllamaHealth literal
                "url": "x",
                "model_count": None,
                "error": None,
            }
        )
