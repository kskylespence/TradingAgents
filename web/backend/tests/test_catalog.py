"""Catalog router tests.

Covers:
- All four endpoints return 200 with correctly-shaped JSON.
- ``/analysts?asset_type=crypto`` excludes Fundamentals.
- ``/models?provider=openai&mode=quick`` returns at least one model.
- Unauthenticated requests return 401 — but ONLY once the AUTH team's
  ``app.auth.get_current_user`` is in place. While the catalog router
  falls back to a stub dep, an unauthenticated request returns 200 and
  the auth assertion is skipped.

Tests do NOT depend on which auth scheme AUTH lands; they overload the
``get_current_user`` dependency via FastAPI's dependency-override hook
to inject a synthetic user for the happy-path cases.
"""

from __future__ import annotations

from tests.helpers import make_auth_user

import importlib.util

import pytest
from fastapi.testclient import TestClient


def _has_real_auth() -> bool:
    """True iff the AUTH team's app.auth.get_current_user has landed."""
    spec = importlib.util.find_spec("app.auth")
    return spec is not None


@pytest.fixture
def authed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient with the catalog router's auth dep stubbed to a user.

    Works whether or not the real ``app.auth`` exists yet: we override
    whichever ``get_current_user`` the catalog router resolved at import
    time, so the same fixture covers both the stub-in-flight and
    AUTH-landed cases.

    Sets ``OLLAMA_BASE_URL`` so the env-based provider filter (added in
    the Ollama Cloud fix) includes ollama in ``/providers``. The other
    provider API keys come from the autouse ``_dummy_api_keys`` fixture
    in ``conftest.py``, so openai/anthropic/google/etc. are also visible.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    from app.main import app
    from app.routers.catalog import get_current_user
    from app.schemas import AuthUser

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# --------------------------------------------------------------------------- #
# Happy-path: shape + content                                                 #
# --------------------------------------------------------------------------- #


def test_providers_returns_200_and_includes_openai_and_ollama(
    authed_client: TestClient,
) -> None:
    resp = authed_client.get("/api/catalog/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and len(body) > 0

    by_key = {p["key"]: p for p in body}

    # Core providers must be present.
    for required in ("openai", "anthropic", "google", "ollama"):
        assert required in by_key, f"missing provider key {required!r}"

    # Every entry has the required CatalogProvider fields.
    for p in body:
        assert set(p.keys()) >= {
            "key",
            "label",
            "regions",
            "requires_api_key",
            "api_key_env",
        }
        assert isinstance(p["requires_api_key"], bool)
        assert isinstance(p["api_key_env"], str)

    # Ollama is the only provider that doesn't require an API key.
    assert by_key["ollama"]["requires_api_key"] is False
    assert by_key["openai"]["requires_api_key"] is True
    # OpenAI's env var name is canonical.
    assert by_key["openai"]["api_key_env"] == "OPENAI_API_KEY"


def test_models_openai_quick_returns_at_least_one_entry(
    authed_client: TestClient,
) -> None:
    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "openai", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    for m in body:
        assert set(m.keys()) == {"id", "label", "allows_custom"}
        assert isinstance(m["allows_custom"], bool)
    # OpenAI's static catalog has no "custom" entry, so allows_custom is
    # False for every model — there is no synthetic __custom__ tail entry.
    assert all(m["id"] != "__custom__" for m in body)


def test_models_deepseek_quick_includes_synthetic_custom_entry(
    authed_client: TestClient,
) -> None:
    """Providers whose model_catalog entry lists ``"custom"`` get a
    synthetic terminal ``{id: "__custom__", allows_custom: True}`` entry.
    """
    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "deepseek", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 2  # at least one real + the synthetic custom
    tail = body[-1]
    assert tail["id"] == "__custom__"
    assert tail["allows_custom"] is True


def test_models_openrouter_falls_back_to_custom_only(
    authed_client: TestClient,
) -> None:
    """OpenRouter isn't in MODEL_OPTIONS — frontend still needs a usable input."""
    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "openrouter", "mode": "deep"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {"id": "__custom__", "label": "Custom model ID", "allows_custom": True}
    ]


def test_models_invalid_mode_rejected(authed_client: TestClient) -> None:
    """Mode Literal validation: only ``quick`` / ``deep`` are accepted."""
    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "openai", "mode": "wrong"}
    )
    assert resp.status_code == 422


def test_analysts_stock_returns_all_four(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/catalog/analysts", params={"asset_type": "stock"}
    )
    assert resp.status_code == 200
    body = resp.json()
    keys = [a["key"] for a in body]
    assert keys == ["market", "social", "news", "fundamentals"]


def test_analysts_crypto_excludes_fundamentals(authed_client: TestClient) -> None:
    """``filter_analysts_for_asset_type`` drops Fundamentals for crypto."""
    resp = authed_client.get(
        "/api/catalog/analysts", params={"asset_type": "crypto"}
    )
    assert resp.status_code == 200
    body = resp.json()
    keys = {a["key"] for a in body}
    assert "fundamentals" not in keys
    # The other three remain.
    assert {"market", "social", "news"} <= keys


def test_analysts_social_displays_as_sentiment_analyst(
    authed_client: TestClient,
) -> None:
    """Wire key ``social`` MUST display as ``Sentiment Analyst`` (CLAUDE.md)."""
    resp = authed_client.get(
        "/api/catalog/analysts", params={"asset_type": "stock"}
    )
    assert resp.status_code == 200
    by_key = {a["key"]: a for a in resp.json()}
    assert by_key["social"]["label"] == "Sentiment Analyst"


def test_analysts_invalid_asset_type_rejected(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/catalog/analysts", params={"asset_type": "bonds"}
    )
    assert resp.status_code == 422


def test_languages_returns_exactly_eleven(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/catalog/languages")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 11
    keys = [lang["key"] for lang in body]
    # First is English; the set covers the documented 11.
    assert keys[0] == "English"
    assert set(keys) == {
        "English",
        "Chinese",
        "Japanese",
        "Korean",
        "Hindi",
        "Spanish",
        "Portuguese",
        "French",
        "German",
        "Arabic",
        "Russian",
    }


# --------------------------------------------------------------------------- #
# Auth gating                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not _has_real_auth(),
    reason="AUTH team's app.auth.get_current_user has not landed yet; "
    "catalog router falls back to a stub that accepts anonymous calls.",
)
def test_unauthenticated_request_returns_401() -> None:
    """Without a JWT cookie, every catalog endpoint returns 401.

    Skipped until ``app.auth`` exists — the catalog router's import-time
    fallback is intentionally permissive so it stays compilable in
    isolation.
    """
    from app.main import app

    client = TestClient(app)
    for path, params in (
        ("/api/catalog/providers", {}),
        ("/api/catalog/models", {"provider": "openai", "mode": "quick"}),
        ("/api/catalog/analysts", {"asset_type": "stock"}),
        ("/api/catalog/languages", {}),
    ):
        resp = client.get(path, params=params)
        assert resp.status_code == 401, (
            f"{path} returned {resp.status_code}, expected 401"
        )
