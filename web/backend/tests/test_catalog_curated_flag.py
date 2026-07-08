"""Catalog ``/models?provider=ollama`` must mark each model as curated or not.

Why a ``curated: bool`` field, not a filter:
  Ollama Cloud's ``/v1/models`` endpoint advertises every model the
  account has access to, including some that the **curated** cloud
  catalog at https://ollama.com/search?c=cloud quietly de-emphasises.
  Two of those (``kimi-k2-thinking``, ``qwen3-coder:480b``) have
  publicly tracked reliability issues — ollama/ollama#15453 reports a
  95% failure rate, and #14542 covers tool-call 500s. Hiding them
  silently would break power users who deliberately picked them; the
  middle path is to keep them in the dropdown but sort them to the
  bottom and badge them so the user can make an informed choice.

  The curated set is a snapshot of the upstream catalog as of
  2026-05-23. See ``app.services.ollama_curated`` for the policy and
  refresh cadence.

Non-Ollama providers MUST NOT carry the field — their model catalogs
come from a hardcoded list in ``tradingagents.llm_clients.model_catalog``
and have no notion of "curated upstream". Leaking the field there would
imply a quality signal we don't actually have.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .conftest import install_fake_httpx_ollama


@pytest.fixture
def authed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from app.main import app
    from app.routers.catalog import get_current_user
    from app.schemas import AuthUser

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    app.dependency_overrides[get_current_user] = lambda: AuthUser(username="tester")
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# --------------------------------------------------------------------------- #
# Service-layer unit test (no FastAPI dep)                                    #
# --------------------------------------------------------------------------- #


def test_is_curated_function() -> None:
    """``is_curated`` is the single source-of-truth predicate.

    Both the catalog router and any future health/diagnostic code
    should call this rather than re-inlining the membership check, so
    the curated snapshot lives in exactly one place.
    """
    from app.services.ollama_curated import is_curated

    # A known-curated model (in the 2026-05-23 snapshot).
    assert is_curated("glm-5.2") is True
    assert is_curated("glm-5") is True
    # A known-deprioritised model (ollama/ollama#15453).
    assert is_curated("kimi-k2-thinking") is False
    # Empty string — defensive: a malformed upstream entry shouldn't
    # accidentally look curated.
    assert is_curated("") is False


# --------------------------------------------------------------------------- #
# /api/catalog/models?provider=ollama happy paths                             #
# --------------------------------------------------------------------------- #


def test_curated_model_marked_true(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Models listed in the curated snapshot are flagged ``curated: true``."""
    install_fake_httpx_ollama(monkeypatch, ids=["glm-5"])

    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "glm-5"
    assert body[0]["curated"] is True


def test_deprioritized_model_marked_false(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Models NOT in the curated snapshot are flagged ``curated: false``.

    ``kimi-k2-thinking`` is the canonical example — it's the model that
    triggered ollama/ollama#15453 (95% failure rate).
    """
    install_fake_httpx_ollama(monkeypatch, ids=["kimi-k2-thinking"])

    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "kimi-k2-thinking"
    assert body[0]["curated"] is False


def test_unknown_model_marked_false(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown models default to ``curated: false`` (conservative).

    A new model the snapshot hasn't seen yet is treated like a
    deprioritised one — the user is shown the safer-known options
    first. The cost of mislabelling a good model as non-curated is a
    small UI badge; the cost of the inverse (marking ``kimi-k2-thinking``
    curated) is a frustrated user.
    """
    install_fake_httpx_ollama(monkeypatch, ids=["random-model:99"])

    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "ollama", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "random-model:99"
    assert body[0]["curated"] is False


# --------------------------------------------------------------------------- #
# Non-ollama providers MUST NOT carry the field                                #
# --------------------------------------------------------------------------- #


def test_non_ollama_providers_unaffected(authed_client: TestClient) -> None:
    """The ``curated`` field is Ollama-specific.

    Other providers have a hardcoded model catalog and no notion of
    "in the curated upstream"; emitting the field there would imply a
    quality signal that doesn't exist. The frontend type makes the
    field optional so the absence here is exactly the back-compat
    path it expects.
    """
    resp = authed_client.get(
        "/api/catalog/models", params={"provider": "openai", "mode": "quick"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) >= 1
    for entry in body:
        # Either the key is omitted entirely, or it's None — both shapes
        # are acceptable to the typed frontend (``curated?: boolean``).
        assert entry.get("curated") in (None, True, False)
        # If present at all, we treat it as a bug — non-ollama emits a
        # quality signal we don't have. Tighten the assertion to "absent".
        assert "curated" not in entry or entry["curated"] is None
