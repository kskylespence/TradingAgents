"""Router registry — Phase 2 agents append their `APIRouter` here.

To add a router::

    # in app/routers/my_router.py
    from fastapi import APIRouter
    from . import register

    router = APIRouter(prefix="/foo", tags=["foo"])

    @router.get("/")
    async def index(): ...

    register(router)

Then add `from .routers import my_router  # noqa: F401` to app/main.py so
the module is imported (which triggers `register()`).

`main.py` iterates `ROUTERS` after all imports and `include_router`s each.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter

ROUTERS: List[APIRouter] = []


def register(router: APIRouter) -> None:
    """Append a router to the registry; main.py will include it under /api."""
    ROUTERS.append(router)


__all__ = ["ROUTERS", "register"]
