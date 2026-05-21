"""Tests for the SecurityHeadersMiddleware.

Verifies the response-side header injection on a real route. Uses the
bootstrap health endpoint as a stable target — it exists from day one
in `app/main.py` and survives the foundation smoke test.

HSTS has two cases (off when http, on when X-Forwarded-Proto: https
is present) — both are exercised here.
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
    # No X-Forwarded-Proto, no https scheme → no HSTS header.
    assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}


def test_hsts_present_when_x_forwarded_proto_https(client: TestClient) -> None:
    """Coolify's Traefik sets X-Forwarded-Proto: https — that triggers HSTS."""
    from app.middleware.security_headers import HSTS_VALUE

    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    assert resp.headers.get("strict-transport-security") == HSTS_VALUE


def test_hsts_present_when_x_forwarded_proto_https_with_list(client: TestClient) -> None:
    """Comma-separated XFP (chained proxies) — first value wins."""
    from app.middleware.security_headers import HSTS_VALUE

    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "https, http"})
    assert resp.status_code == 200
    assert resp.headers.get("strict-transport-security") == HSTS_VALUE


def test_hsts_absent_when_x_forwarded_proto_http(client: TestClient) -> None:
    """X-Forwarded-Proto: http explicitly means the original was plaintext."""
    resp = client.get("/api/health", headers={"X-Forwarded-Proto": "http"})
    assert resp.status_code == 200
    assert "strict-transport-security" not in {k.lower() for k in resp.headers.keys()}
