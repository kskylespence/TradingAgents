"""Tests for the CSRFMiddleware (double-submit cookie).

Strategy: build a fresh FastAPI app and install only the CSRF
middleware (so registry side-effects from the production app, e.g. the
auth router which may add additional exemptions later, don't interfere
with the focused test).

We also smoke-test against the real `app.main:app` to verify that the
exempt path constant matches what the auth login route will use, and
that the CSRF check doesn't accidentally fire on GET /api/health.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    EXEMPT_PATHS,
)


@pytest.fixture
def isolated_client() -> TestClient:
    """A throwaway app with just CSRFMiddleware + a few echo routes.

    Avoids coupling to whatever routes/middleware the production app
    happens to have registered today.
    """
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.get("/echo")
    async def echo_get() -> dict:
        return {"ok": True, "method": "GET"}

    @app.post("/echo")
    async def echo_post() -> dict:
        return {"ok": True, "method": "POST"}

    @app.put("/echo")
    async def echo_put() -> dict:
        return {"ok": True, "method": "PUT"}

    @app.delete("/echo")
    async def echo_delete() -> dict:
        return {"ok": True, "method": "DELETE"}

    @app.patch("/echo")
    async def echo_patch() -> dict:
        return {"ok": True, "method": "PATCH"}

    @app.post("/api/auth/login")
    async def fake_login() -> dict:
        return {"ok": True, "exempt": True}

    with TestClient(app) as client:
        yield client


# --- Safe-method bypass --------------------------------------------------- #


def test_get_request_bypasses_csrf(isolated_client: TestClient) -> None:
    """GET is a safe method — no CSRF check ever, even with no cookie/header."""
    resp = isolated_client.get("/echo")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "method": "GET"}


def test_options_request_bypasses_csrf(isolated_client: TestClient) -> None:
    """OPTIONS preflight is safe — must never 403 (CORS would break)."""
    # FastAPI may 405 OPTIONS on a non-CORS route, but it must not 403.
    resp = isolated_client.request("OPTIONS", "/echo")
    assert resp.status_code != 403


# --- State-changing methods: enforcement -------------------------------- #


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_state_change_without_token_is_403(
    isolated_client: TestClient, method: str
) -> None:
    """No cookie, no header → 403."""
    resp = isolated_client.request(method, "/echo")
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_state_change_with_header_but_no_cookie_is_403(
    isolated_client: TestClient,
) -> None:
    """Header without matching cookie → 403 (attacker can't read victim cookie)."""
    resp = isolated_client.post(
        "/echo",
        headers={CSRF_HEADER_NAME: "some-attacker-supplied-value"},
    )
    assert resp.status_code == 403


def test_state_change_with_cookie_but_no_header_is_403(
    isolated_client: TestClient,
) -> None:
    """Cookie without matching header → 403 (classic CSRF posts cookies but not headers)."""
    isolated_client.cookies.set(CSRF_COOKIE_NAME, "legitimate-token")
    try:
        resp = isolated_client.post("/echo")
        assert resp.status_code == 403
    finally:
        isolated_client.cookies.clear()


def test_state_change_with_mismatched_token_is_403(
    isolated_client: TestClient,
) -> None:
    """Cookie value != header value → 403."""
    isolated_client.cookies.set(CSRF_COOKIE_NAME, "real-token")
    try:
        resp = isolated_client.post(
            "/echo",
            headers={CSRF_HEADER_NAME: "different-token"},
        )
        assert resp.status_code == 403
    finally:
        isolated_client.cookies.clear()


def test_state_change_with_matching_token_succeeds(
    isolated_client: TestClient,
) -> None:
    """Matching cookie + header → request reaches the handler."""
    token = "matching-token-abc-123"
    isolated_client.cookies.set(CSRF_COOKIE_NAME, token)
    try:
        resp = isolated_client.post(
            "/echo",
            headers={CSRF_HEADER_NAME: token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
    finally:
        isolated_client.cookies.clear()


def test_empty_token_strings_are_rejected(isolated_client: TestClient) -> None:
    """Both empty cookie and empty header → 403, not accidental pass."""
    isolated_client.cookies.set(CSRF_COOKIE_NAME, "")
    try:
        resp = isolated_client.post("/echo", headers={CSRF_HEADER_NAME: ""})
        assert resp.status_code == 403
    finally:
        isolated_client.cookies.clear()


# --- Login exemption ---------------------------------------------------- #


def test_login_endpoint_is_exempt(isolated_client: TestClient) -> None:
    """POST /api/auth/login must work without CSRF — login SETS the cookie.

    This is the only exempt state-changing path (see EXEMPT_PATHS).
    """
    resp = isolated_client.post("/api/auth/login", json={"u": "x", "p": "y"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "exempt": True}


def test_login_is_the_only_exempt_path() -> None:
    """Defensive check on the exemption set — keep it tight on purpose."""
    assert EXEMPT_PATHS == frozenset({"/api/auth/login"})


# --- Smoke against the real production app -------------------------------- #


def test_real_app_health_get_is_not_blocked_by_csrf() -> None:
    """The production `app.main:app` must still serve GET /api/health.

    A CSRF middleware that fires on GETs would brick health-checks and
    the entire SPA. This is the load-bearing smoke from the brief.
    """
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
