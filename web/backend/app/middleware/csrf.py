"""Double-submit cookie CSRF protection.

The frontend reads the non-HttpOnly ``csrf_token`` cookie set at login
and echoes its value in the ``X-CSRF-Token`` header on every
state-changing request. This middleware checks the two match.

Why double-submit cookie:
- Cheap (no server-side state).
- Effective against classic CSRF: an attacker on a sibling origin cannot
  read the ``csrf_token`` cookie value cross-origin, so they cannot
  forge the matching header.
- Belt-and-suspenders alongside SameSite=Lax on the auth cookie.

Enforcement rules:
- Safe methods (``GET``, ``HEAD``, ``OPTIONS``) are NEVER checked — by
  definition they should not change server state, and many tools/probes
  legitimately use them without setting the header.
- ``POST /api/auth/login`` is exempt. The login flow is what *sets* the
  CSRF cookie in the first place, so requiring one to log in is a
  chicken-and-egg deadlock. This is the only exempted state-changing
  path; the SAFE_PATHS constant is deliberately a small explicit set
  rather than a regex/prefix to make the exemption auditable.

CSP/security-headers ordering: this middleware short-circuits the
request before it reaches a handler (returns 403 directly). The
SecurityHeaders middleware acts on the response, so it can sit on
either side — order between the two does not matter for correctness.
"""

from __future__ import annotations

from typing import Iterable

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import register


CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
STATE_CHANGING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Exempt paths — see module docstring. The login endpoint SETS the cookie,
# so requiring the cookie before login is a chicken-and-egg deadlock. Keep
# this set tiny and explicit; no prefix/regex matching.
EXEMPT_PATHS: frozenset[str] = frozenset({"/api/auth/login"})


def _csrf_required(method: str, path: str) -> bool:
    """Return True if this (method, path) must pass the CSRF check."""
    if method.upper() not in STATE_CHANGING_METHODS:
        return False
    if path in EXEMPT_PATHS:
        return False
    return True


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce double-submit cookie on state-changing requests."""

    def __init__(self, app, exempt_paths: Iterable[str] | None = None) -> None:
        super().__init__(app)
        # Allow tests / future callers to add extra exemptions without
        # editing the module-level constant.
        self._exempt: frozenset[str] = (
            EXEMPT_PATHS if exempt_paths is None else frozenset(exempt_paths) | EXEMPT_PATHS
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        if not _csrf_required(request.method, request.url.path):
            return await call_next(request)
        # State-changing & not exempt: cookie and header must both exist
        # AND match exactly.
        header_token = request.headers.get(CSRF_HEADER_NAME)
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
        if not header_token or not cookie_token or header_token != cookie_token:
            return JSONResponse(
                {"detail": "CSRF token missing or invalid"},
                status_code=403,
            )
        return await call_next(request)


def install(app: FastAPI) -> None:
    """Attach the CSRFMiddleware to the FastAPI app."""
    app.add_middleware(CSRFMiddleware)


register(install)


__all__ = [
    "CSRFMiddleware",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "STATE_CHANGING_METHODS",
    "EXEMPT_PATHS",
    "install",
]
