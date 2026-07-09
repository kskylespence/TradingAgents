"""Tests for the announcements proxy service + router.

The proxy mirrors `cli/announcements.py`'s error posture: on ANY failure
(timeout, network, malformed JSON, HTTP error), return an empty list and
never propagate the exception. Successful payloads are cached in-process
for 60s.

These tests mock `httpx.AsyncClient.get` rather than hitting the network
so they are deterministic + offline.
"""

from __future__ import annotations

from tests.helpers import make_auth_user

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Override the auth dependency for these tests.

    The router requires a logged-in user via ``get_current_user``. We
    don't exercise auth here (that's the AUTH team's suite); short-circuit
    it so we can test the proxy behavior in isolation.
    """
    from app.main import app

    # Import the exact callable the router depends on so the override
    # key matches FastAPI's dependency identity check.
    from app.routers.announcements import get_current_user
    from app.schemas import AuthUser

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="test-user")
    yield
    app.dependency_overrides.pop(get_current_user, None)


SAMPLE_PAYLOAD = [
    {
        "id": "a1",
        "title": "Welcome",
        "body": "TradingAgents v1 is live.",
        "url": "https://tauric.ai/news/a1",
        "severity": "info",
        "published_at": "2026-05-19T12:00:00Z",
    },
    {
        "id": "a2",
        "title": "Heads up",
        "body": "Provider quota changes coming.",
        "severity": "warning",
    },
]


def _make_response(*, status_code: int = 200, json_body=None, text: str | None = None) -> httpx.Response:
    """Build a real httpx.Response so callers exercise the same parsing path."""
    request = httpx.Request("GET", "https://api.tauric.ai/v1/announcements")
    if text is not None:
        content = text.encode("utf-8")
    else:
        content = json.dumps(json_body if json_body is not None else []).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        request=request,
        content=content,
        headers={"content-type": "application/json"},
    )


@pytest.fixture(autouse=True)
def _reset_announcements_cache():
    """Wipe the module-level cache between tests so each runs in isolation."""
    from app.services import announcements as svc

    svc._reset_cache_for_tests()
    yield
    svc._reset_cache_for_tests()


def test_endpoint_returns_parsed_list_on_success() -> None:
    """A 200 with a valid payload comes back as a list[Announcement] JSON."""
    from app.main import app

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=_make_response(json_body=SAMPLE_PAYLOAD))):
        with TestClient(app) as client:
            resp = client.get("/api/announcements/")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["id"] == "a1"
    assert body[0]["title"] == "Welcome"
    assert body[1]["severity"] == "warning"


def test_endpoint_returns_empty_on_timeout() -> None:
    """httpx.TimeoutException → empty list, no 5xx leak."""
    from app.main import app

    with patch(
        "httpx.AsyncClient.get",
        new=AsyncMock(side_effect=httpx.TimeoutException("slow upstream")),
    ), TestClient(app) as client:
        resp = client.get("/api/announcements/")

    assert resp.status_code == 200
    assert resp.json() == []


def test_endpoint_returns_empty_on_malformed_json() -> None:
    """Body that isn't valid JSON → empty list, exception swallowed."""
    from app.main import app

    bad = _make_response(text="<html>not json</html>")

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=bad)):
        with TestClient(app) as client:
            resp = client.get("/api/announcements/")

    assert resp.status_code == 200
    assert resp.json() == []


def test_endpoint_returns_empty_on_http_error() -> None:
    """A 5xx response is treated as failure → empty list."""
    from app.main import app

    err = _make_response(status_code=503, json_body={"error": "down"})

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=err)):
        with TestClient(app) as client:
            resp = client.get("/api/announcements/")

    assert resp.status_code == 200
    assert resp.json() == []


def test_second_call_within_ttl_hits_cache() -> None:
    """Two requests within 60s share the cached payload; upstream called once."""
    from app.main import app

    mock_get = AsyncMock(return_value=_make_response(json_body=SAMPLE_PAYLOAD))

    with patch("httpx.AsyncClient.get", new=mock_get), TestClient(app) as client:
        r1 = client.get("/api/announcements/")
        r2 = client.get("/api/announcements/")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    assert mock_get.call_count == 1, (
        f"Expected one upstream call within TTL, got {mock_get.call_count}"
    )


def test_cache_expires_after_ttl(monkeypatch) -> None:
    """Once TTL elapses, the next call re-fetches from upstream."""
    from app.main import app
    from app.services import announcements as svc

    mock_get = AsyncMock(return_value=_make_response(json_body=SAMPLE_PAYLOAD))

    # Freeze "now" so we can jump it forward past TTL.
    fake_now = {"t": 1_000_000.0}

    def _now() -> float:
        return fake_now["t"]

    monkeypatch.setattr(svc, "_now", _now)

    with patch("httpx.AsyncClient.get", new=mock_get), TestClient(app) as client:
        client.get("/api/announcements/")
        fake_now["t"] += svc.CACHE_TTL_SECONDS + 1.0
        client.get("/api/announcements/")

    assert mock_get.call_count == 2
