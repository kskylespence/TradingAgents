"""FastAPI app entrypoint.

Routers are pluggable: every submodule of `app.routers` calls
`routers.register(router)` at import time; this module imports those
submodules and iterates the registry to include them all under `/api`.

Background tasks (disk pruner from task #6, crash recovery from task #5)
are wired into the lifespan as they land. Today the lifespan only
configures logging and disposes the DB engine on shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import dispose_engine
from .logging_config import configure_logging
from . import routers as routers_registry
from . import middleware as middleware_registry
from . import lifespan_hooks as lifespan_registry

# --- Router auto-discovery ---
# `app.routers` auto-imports every submodule on package load (see
# routers/__init__.py:_autoload). Each module calls `register(router)` at
# module scope, populating `routers_registry.ROUTERS`. Parallel agents can
# therefore add new router files without touching this file.


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup + shutdown lifecycle.

    Lifespan hooks live in `app/lifespan_hooks/` — each submodule decorates
    its callbacks with `@on_startup` / `@on_shutdown`. Startup hooks run in
    registration order; shutdown hooks run in reverse registration order so
    teardown unwinds setup. Shutdown errors are logged and swallowed so the
    rest of teardown still completes.
    """
    configure_logging()
    log.info("backend.startup", extra={"app_env": get_settings().app_env})
    for hook in lifespan_registry.STARTUP_HOOKS:
        await hook(app)
    try:
        yield
    finally:
        log.info("backend.shutdown")
        for hook in reversed(lifespan_registry.SHUTDOWN_HOOKS):
            try:
                await hook(app)
            except Exception:
                log.exception("backend.shutdown_hook_failed", extra={"hook": hook.__name__})
        await dispose_engine()


def create_app() -> FastAPI:
    """Application factory. Useful for tests and ASGI deployment."""
    settings = get_settings()
    app = FastAPI(
        lifespan=lifespan,
        title=settings.app_title,
        debug=settings.debug,
        # Tighten the default docs paths — only enabled in dev. Phase-2
        # task #4 (health) will override this if it wants to.
        docs_url="/api/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.debug else None,
    )

    # Placeholder health endpoint so the smoke test has something to hit
    # before the catalog/health router (task #4) lands. The real router
    # will register `/health` and FastAPI's last-registered-wins behavior
    # means the placeholder is silently superseded once task #4 imports.
    @app.get("/api/_bootstrap_health", tags=["bootstrap"])
    async def _bootstrap_health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # Mirror at /api/health too so a curl-style smoke test against the
    # canonical path works before task #4 ships. Once task #4 registers
    # its own `GET /health` router under `/api`, FastAPI's matching will
    # prefer the included router's route since it's added later.
    @app.get("/api/health", tags=["bootstrap"])
    async def _placeholder_health() -> JSONResponse:
        return JSONResponse({"status": "ok", "placeholder": True})

    # Install registered middleware (FastAPI applies in reverse registration
    # order; the LAST add_middleware wraps the request first).
    middleware_registry.install_all(app)

    # Include every registered router under `/api`.
    for router in routers_registry.ROUTERS:
        app.include_router(router, prefix="/api")

    # Static fallback: the React build is copied to app/static at
    # image-build time. Mount LAST so /api/* takes precedence. `html=True`
    # makes StaticFiles serve `index.html` on directory hits (SPA needs
    # this for client-side routing).
    if settings.static_dir.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(settings.static_dir), html=True),
            name="static",
        )

    return app


app = create_app()
