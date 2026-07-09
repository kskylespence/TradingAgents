"""Router registry with auto-discovery.

Any submodule added to `app/routers/` (whose name does not start with `_`)
is automatically imported when this package is imported. Each router module
calls `register(router)` at module scope, so the side-effect of the import
populates `ROUTERS` without any further wiring in `main.py`.

To add a router::

    # in app/routers/my_router.py
    from fastapi import APIRouter
    from . import register

    router = APIRouter(prefix="/foo", tags=["foo"])

    @router.get("/")
    async def index(): ...

    register(router)

That's it — no edits to `main.py` or this file are required. Parallel
agents authoring different routers therefore never touch shared files.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter

ROUTERS: list[APIRouter] = []


def register(router: APIRouter) -> None:
    """Append a router to the registry; main.py will include it under /api."""
    ROUTERS.append(router)


def _autoload() -> None:
    """Import every submodule of this package so registrations fire.

    Modules whose names start with `_` (e.g. `_imports`) are skipped, by
    convention reserved for private helpers that should not register
    routes themselves.
    """
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{name}")


_autoload()


__all__ = ["ROUTERS", "register"]
