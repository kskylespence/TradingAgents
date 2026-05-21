"""Catalog /providers must filter by env-credential presence.

After this PR, `/api/catalog/providers` is no longer "every provider we
know how to wire up" — it's "every provider we have credentials for in
this deployment." The frontend dropdown then only shows providers that
can actually run.

The autouse `_dummy_api_keys` fixture from `conftest.py` sets all
provider API keys to ``"placeholder"`` so the engine doesn't try to
talk to a real provider during tests; we explicitly delenv the ones we
want absent on a per-test basis to defeat that fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV


_ALL_PROVIDER_ENV_VARS: tuple[str, ...] = tuple(
    sorted(
        {env for env in PROVIDER_API_KEY_ENV.values() if env}
        | {"AZURE_OPENAI_ENDPOINT", "OLLAMA_BASE_URL"}
    )
)


def _clear_all_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in _ALL_PROVIDER_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


@pytest.fixture
def authed_client() -> TestClient:
    from app.main import app
    from app.routers.catalog import get_current_user
    from app.schemas import AuthUser

    app.dependency_overrides[get_current_user] = lambda: AuthUser(username="tester")
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_only_ollama_appears_when_only_ollama_base_url_set(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    resp = authed_client.get("/api/catalog/providers")
    assert resp.status_code == 200

    body = resp.json()
    keys = [p["key"] for p in body]
    assert keys == ["ollama"], f"expected only ollama, got {keys}"


def test_empty_when_no_credentials_set(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_provider_env(monkeypatch)

    resp = authed_client.get("/api/catalog/providers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_two_providers_when_both_credentials_set(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    resp = authed_client.get("/api/catalog/providers")
    assert resp.status_code == 200

    keys = {p["key"] for p in resp.json()}
    assert keys == {"ollama", "openai"}


def test_azure_excluded_when_endpoint_missing(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure")
    # AZURE_OPENAI_ENDPOINT intentionally absent.

    resp = authed_client.get("/api/catalog/providers")
    assert resp.status_code == 200
    keys = {p["key"] for p in resp.json()}
    assert "azure" not in keys
