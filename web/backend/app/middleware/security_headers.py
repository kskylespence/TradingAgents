"""Security response headers middleware.

Adds the standard browser-hardening headers to every response:

- ``Content-Security-Policy`` — restrict resource origins to self; allow
  inline scripts/styles for shadcn/Tailwind (tighten later via nonces).
- ``Strict-Transport-Security`` — only when the request actually arrived
  over HTTPS. Coolify's Traefik proxy terminates TLS, so the app sees
  ``http`` even on prod; we check ``X-Forwarded-Proto`` as well as
  ``request.url.scheme`` to honor the original scheme.
- ``X-Frame-Options: DENY`` — defense-in-depth against clickjacking.
- ``X-Content-Type-Options: nosniff`` — disable MIME sniffing.
- ``Referrer-Policy: strict-origin-when-cross-origin`` — leak as little
  referrer as possible cross-origin.

This middleware is purely additive on the response side, so its ordering
relative to other middleware (e.g. CSRF) does not matter for correctness.
Per ``app/middleware/__init__.py`` ordering rules, it is registered the
same way as any other middleware and applied in reverse-registration
order by FastAPI/Starlette.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import register


CSP_VALUE = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self';"
)
HSTS_VALUE = "max-age=63072000; includeSubDomains; preload"


def _is_https(request: Request) -> bool:
    """Return True if the original client request was HTTPS.

    Coolify's reverse proxy terminates TLS and forwards ``http://`` to the
    app, so the app's view of ``request.url.scheme`` will be ``http`` even
    on production. The proxy MUST set ``X-Forwarded-Proto: https`` for us
    to know — we trust that header here because the deployment topology
    guarantees the proxy is the only thing in front of us.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    # Header may be a comma-separated list (RFC 7239-ish behavior); take
    # the first value as the originating scheme.
    first = forwarded.split(",", 1)[0].strip().lower()
    return first == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Inject security headers onto every outgoing response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        headers = response.headers
        # `setdefault`-style: do not overwrite headers a route handler
        # explicitly set (e.g. a route choosing a stricter CSP).
        headers.setdefault("Content-Security-Policy", CSP_VALUE)
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if _is_https(request):
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response


def install(app: FastAPI) -> None:
    """Attach the SecurityHeadersMiddleware to the FastAPI app."""
    app.add_middleware(SecurityHeadersMiddleware)


register(install)


__all__ = ["SecurityHeadersMiddleware", "CSP_VALUE", "HSTS_VALUE", "install"]
