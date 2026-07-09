"""Catalog /models for provider=ollama must come from live upstream.

The hardcoded Ollama tags in `tradingagents/llm_clients/model_catalog.py`
were *local-Ollama* names (e.g. `qwen3:latest`) — they don't exist on
Ollama Cloud, which is what production deploys to (`OLLAMA_BASE_URL=
https://ollama.com/v1`). The fix is to query the upstream
`/v1/models` endpoint and serve whatever IDs come back. Both Ollama
local and Ollama Cloud implement the OpenAI-compatible list endpoint,
so the same call works for both.

No `__custom__` sentinel for ollama — the user's explicit directive was
"we don't want the frontend user being able to choose something that
doesn't work."
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_auth_user

from .conftest import install_fake_httpx_ollama


@pytest.fixture
def authed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.main import app
    from app.routers.catalog import get_current_user

    # Ensure ollama is configured so the filter doesn't strip it.
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch, data: list[dict]
) -> None:
    """Thin wrapper — translates the older list-of-dicts shape into shared helper kwargs."""
    install_fake_httpx_ollama(monkeypatch, ids=[d["id"] for d in data])


def test_ollama_models_quick_returns_live_ids(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_httpx(
        monkeypatch,
        [
            {"id": "gpt-oss:120b", "object": "model"},
            {"id": "qwen3-coder:480b", "object": "model"},
            {"id": "glm-4.7", "object": "model"},
        ],
    )

    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()

    ids = [m["id"] for m in body]
    assert ids == ["gpt-oss:120b", "qwen3-coder:480b", "glm-4.7"]

    # No synthetic __custom__ entry for ollama — frontend is restricted to
    # discovered models only.
    assert "__custom__" not in ids
    for m in body:
        assert m["allows_custom"] is False


def test_ollama_models_quick_and_deep_return_same_list(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama doesn't distinguish quick vs deep — both modes return the same set."""
    _install_fake_httpx(
        monkeypatch,
        [
            {"id": "gpt-oss:120b", "object": "model"},
            {"id": "qwen3-coder:480b", "object": "model"},
        ],
    )

    q = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    ).json()
    d = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "deep"}
    ).json()
    assert q == d


def test_ollama_models_empty_upstream_returns_empty(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream returns no models -> dropdown is empty (frontend will disable it)."""
    _install_fake_httpx(monkeypatch, [])

    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    )
    assert resp.status_code == 200
    assert resp.json() == []
