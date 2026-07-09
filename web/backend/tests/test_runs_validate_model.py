"""POST /api/runs rejects invalid provider/model pairs with 400.

Defense in depth — even if the form somehow submits stale state
(replayed POST, stale browser tab, direct curl), the run must NOT
start. The existing failure mode was a 10-second-late 500 from inside
the engine (Ollama returns 404 from chat/completions). After this
change the failure is a 400 before any state is persisted and before
the engine launches.

`start_run` MUST NOT be called when validation fails.
"""

from __future__ import annotations

import pytest
from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from fastapi.testclient import TestClient

from .conftest import install_fake_httpx_ollama as _install_fake_httpx

CSRF_TOKEN = "test-csrf-token-runs"


@pytest.fixture
def runs_client(monkeypatch):
    from uuid import uuid4

    from app.main import app
    from app.routers.runs import get_current_user
    from app.services import run_service

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    # Spy on start_run so we can prove it WAS NOT called on validation failures.
    calls: list[tuple] = []

    async def _spy_start_run(body, db, *, user_id=None):
        calls.append((body, db))
        return uuid4()

    monkeypatch.setattr(run_service, "start_run", _spy_start_run)

    from tests.helpers import make_auth_user

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")

    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client, calls
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _build_body(**overrides) -> dict:
    body = {
        "ticker": "NVDA",
        "analysis_date": "2026-05-21",
        "output_language": "English",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "ollama",
        "quick_think_llm": "gpt-oss:120b",
        "deep_think_llm": "qwen3-coder:480b",
        "enable_checkpoint": False,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict):
    return client.post(
        "/api/runs",
        json=body,
        headers={CSRF_HEADER_NAME: CSRF_TOKEN},
    )


def test_invalid_quick_model_returns_400(
    runs_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls = runs_client
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"])

    resp = _post(client, _build_body(quick_think_llm="qwen3:latest"))

    assert resp.status_code == 400, resp.text
    detail = resp.json().get("detail", "")
    assert "qwen3:latest" in detail
    assert "ollama" in detail.lower()
    assert calls == [], "start_run must NOT be called when validation fails"


def test_invalid_deep_model_returns_400(
    runs_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls = runs_client
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b"])

    resp = _post(client, _build_body(deep_think_llm="glm-4.7-flash:latest"))

    assert resp.status_code == 400, resp.text
    assert "glm-4.7-flash:latest" in resp.json().get("detail", "")
    assert calls == []


def test_valid_models_pass_through(
    runs_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls = runs_client
    _install_fake_httpx(monkeypatch, ids=["gpt-oss:120b", "qwen3-coder:480b"])

    resp = _post(client, _build_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert "run_id" in body
    assert len(calls) == 1, "start_run should be called for a valid request"
