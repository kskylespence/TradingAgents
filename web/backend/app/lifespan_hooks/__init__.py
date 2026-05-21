"""Lifespan-hook registry with auto-discovery.

Any submodule of `app/lifespan_hooks/` (whose name doesn't start with `_`)
is auto-imported on package load. Each module decorates functions with
`@on_startup` and/or `@on_shutdown`. `main.py`'s lifespan runs each
startup hook in registration order, then yields, then each shutdown hook
in reverse order on the way out.

Hooks receive the FastAPI app as a single argument. They MUST be async
(declare with `async def`). Exceptions in startup hooks abort startup
(intentional fail-fast); exceptions in shutdown hooks are logged and
suppressed so the rest of teardown still runs.

Example::

    # app/lifespan_hooks/disk_pruner.py
    import asyncio
    from fastapi import FastAPI
    from . import on_startup, on_shutdown

    _task: asyncio.Task | None = None

    @on_startup
    async def start(app: FastAPI) -> None:
        global _task
        _task = asyncio.create_task(_loop())

    @on_shutdown
    async def stop(app: FastAPI) -> None:
        if _task is not None:
            _task.cancel()
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Awaitable, Callable, List

from fastapi import FastAPI

Hook = Callable[[FastAPI], Awaitable[None]]

STARTUP_HOOKS: List[Hook] = []
SHUTDOWN_HOOKS: List[Hook] = []


def on_startup(func: Hook) -> Hook:
    """Decorator: register an async startup hook."""
    STARTUP_HOOKS.append(func)
    return func


def on_shutdown(func: Hook) -> Hook:
    """Decorator: register an async shutdown hook."""
    SHUTDOWN_HOOKS.append(func)
    return func


def _autoload() -> None:
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{name}")


_autoload()


__all__ = ["Hook", "STARTUP_HOOKS", "SHUTDOWN_HOOKS", "on_startup", "on_shutdown"]
