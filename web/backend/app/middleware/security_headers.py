"""Security response headers middleware.

Adds the standard browser-hardening headers to every response:

- ``Content-Security-Policy`` — restrict resource origins to self.
  ``script-src 'self'`` only (no ``'unsafe-inline'``) because Vite's
  production build emits only external module scripts. ``style-src``
  still allows ``'unsafe-inline'`` because Tailwind/shadcn injects
  inline styles.
- ``Strict-Transport-Security`` — only when the request actually
  arrived over HTTPS as reported by ``request.url.scheme``. In
  production we rely on uvicorn's ``--proxy-headers`` flag (which
  enables Starlette's ProxyHeadersMiddleware) to rewrite the scheme
  from the trusted reverse proxy's ``X-Forwarded-Proto`` header before
  our middleware sees the request. We deliberately do NOT consult the
  raw ``X-Forwarded-Proto`` header in app code, since that would let
  an attacker poison HSTS / cookie ``Secure`` flags by sending the
  header themselves to a directly-exposed uvicorn.
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
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self';"
)
HSTS_VALUE = "max-age=63072000; includeSubDomains; preload"


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
        if request.url.scheme == "https":
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response


def install(app: FastAPI) -> None:
    """Attach the SecurityHeadersMiddleware to the FastAPI app."""
    app.add_middleware(SecurityHeadersMiddleware)


register(install)


__all__ = ["SecurityHeadersMiddleware", "CSP_VALUE", "HSTS_VALUE", "install"]
