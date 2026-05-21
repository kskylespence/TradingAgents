"""Tests for the SecurityHeadersMiddleware.

Verifies the response-side header injection on a real route. Uses the
bootstrap health endpoint as a stable target — it exists from day one
in `app/main.py` and survives the foundation smoke test.

HSTS has two cases (off when http, on when ``request.url.scheme`` is
``https``) — both are exercised here. The app no longer trusts raw
``X-Forwarded-Proto`` headers directly; Starlette's ProxyHeadersMiddleware
(enabled via ``uvicorn --proxy-headers``) rewrites ``request.url.scheme``
when running behind a trusted reverse proxy.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client() -> TestClient:
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_baseline_headers_present_on_every_response(client: TestClient) -> None:
    """CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy on all."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    h = resp.headers
    csp = h.get("content-security-policy")
    assert csp is not None
    # Spot-check the directives we promise — exact value is asserted
    # elsewhere by importing CSP_VALUE.
    assert "default-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "connect-src 'self'" in csp
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_csp_matches_module_constant(client: TestClient) -> None:
    """The wire CSP must match what the module exports — no silent drift."""
    from app.middleware.security_headers import CSP_VALUE

    resp = client.get("/api/health")
    assert resp.headers.get("content-security-policy") == CSP_VALUE


def test_hsts_absent_for_plain_http(client: TestClient) -> None:
    """TestClient defaults to http and no proxy headers; HSTS must not leak."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    # No https scheme → no HSTS header.
    assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}


def test_hsts_present_when_request_scheme_is_https() -> None:
    """When ``request.url.scheme == 'https'`` HSTS must be emitted.

    In production, uvicorn's ``--proxy-headers`` enables Starlette's
    ProxyHeadersMiddleware, which rewrites ``request.url.scheme`` from
    the trusted ``X-Forwarded-Proto`` header. Here we simulate the
    post-rewrite condition by setting TestClient's ``base_url`` to https.
    """
    from app.main import app
    from app.middleware.security_headers import HSTS_VALUE

    with TestClient(app, base_url="https://testserver") as c:
        resp = c.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("strict-transport-security") == HSTS_VALUE


def test_hsts_absent_when_x_forwarded_proto_header_set_without_proxy(
    client: TestClient,
) -> None:
    """Raw X-Forwarded-Proto is no longer trusted by the app directly.

    Without uvicorn's ``--proxy-headers`` in front rewriting the scheme,
    a client-supplied ``X-Forwarded-Proto: https`` header MUST NOT cause
    the app to attach HSTS. Otherwise an attacker can poison the
    victim's HSTS state from an HTTP origin.
    """
    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}


def test_csp_script_src_does_not_allow_unsafe_inline(client: TestClient) -> None:
    """``script-src`` MUST NOT include ``'unsafe-inline'`` — XSS belt+braces.

    The Vite production build emits only external module scripts; there
    is no need to allow inline scripts and doing so destroys the CSP's
    value as a safety net against any future XSS sink (e.g. someone
    adding ``rehype-raw`` to react-markdown).
    """
    resp = client.get("/api/health")
    csp = resp.headers.get("content-security-policy")
    assert csp is not None

    # Pull out the script-src directive and check it specifically. We
    # tolerate other directives (style-src) still containing
    # 'unsafe-inline' — only script-src is forbidden from having it.
    directives = {
        d.strip().split(" ", 1)[0]: d.strip()
        for d in csp.split(";")
        if d.strip()
    }
    script_src = directives.get("script-src", "")
    assert "'unsafe-inline'" not in script_src, (
        f"script-src must not allow 'unsafe-inline'; got: {script_src!r}"
    )


def test_csp_style_src_still_allows_unsafe_inline(client: TestClient) -> None:
    """``style-src`` keeps ``'unsafe-inline'`` — Tailwind/shadcn need it."""
    resp = client.get("/api/health")
    csp = resp.headers.get("content-security-policy")
    assert csp is not None
    directives = {
        d.strip().split(" ", 1)[0]: d.strip()
        for d in csp.split(";")
        if d.strip()
    }
    style_src = directives.get("style-src", "")
    assert "'unsafe-inline'" in style_src, (
        f"style-src must still allow 'unsafe-inline' for Tailwind; got: {style_src!r}"
    )
