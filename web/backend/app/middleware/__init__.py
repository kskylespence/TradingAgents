"""Middleware registry with auto-discovery.

Any submodule of `app/middleware/` (whose name doesn't start with `_`) is
auto-imported on package load. Each module calls `register(installer)`
at module scope, where `installer` is a function that takes the FastAPI
app and calls `app.add_middleware(...)` (or otherwise mutates the app).

This mirrors the router-registry pattern so parallel agents adding
middleware never touch `main.py`.

Example::

    # app/middleware/csrf.py
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from . import register

    class CSRFMiddleware(BaseHTTPMiddleware): ...

    def install(app: FastAPI) -> None:
        app.add_middleware(CSRFMiddleware)

    register(install)

Order: middleware in FastAPI is applied in REVERSE registration order, so
the LAST `add_middleware` call wraps the request first. If your middleware
must run before/after a specific other one, document it in the module
docstring and use a `_priority_*.py` filename convention if needed (these
will sort alphabetically inside `pkgutil.iter_modules`).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Callable, List

from fastapi import FastAPI

Installer = Callable[[FastAPI], None]

INSTALLERS: List[Installer] = []


def register(installer: Installer) -> None:
    """Add a middleware installer; main.py calls each one with the app."""
    INSTALLERS.append(installer)


def install_all(app: FastAPI) -> None:
    """Apply every registered installer to `app`. Called from main.py."""
    for installer in INSTALLERS:
        installer(app)


def _autoload() -> None:
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        if name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{name}")


_autoload()


__all__ = ["INSTALLERS", "Installer", "install_all", "register"]
