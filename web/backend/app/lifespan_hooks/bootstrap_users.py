"""Bootstrap application users on startup."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from ..services.users import bootstrap_users
from . import on_startup

log = logging.getLogger(__name__)


@on_startup
async def sync_users(_app: FastAPI) -> None:
    """Upsert admin from env and seed rob@rob when configured."""
    try:
        await bootstrap_users()
    except Exception:
        log.exception("bootstrap_users.failed")
        raise


__all__ = ["sync_users"]
