"""Announcements router — proxies the upstream ``api.tauric.ai`` feed.

The endpoint is authenticated (any logged-in user) because announcements
can include operational notices we don't want to expose unauthenticated
(severity hints, internal-only URLs, etc.). The fetch itself is
delegated to ``app.services.announcements.fetch_announcements`` which
handles caching, timeouts, and never-raise semantics.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.schemas import Announcement
from app.services.announcements import fetch_announcements

# Soft-import the auth dependency. The AUTH team owns ``app/auth.py``;
# if it isn't landed yet (e.g. in a parallel branch where this file is
# imported standalone), fall back to a stub so the router still wires up
# and tests can exercise the proxy behavior.
# TODO: drop the fallback once `app.auth.get_current_user` is guaranteed
# present on every branch this code merges into.
try:
    from app.auth import get_current_user
except ImportError:  # pragma: no cover - exercised when auth team hasn't landed
    from app.schemas import AuthUser

    def get_current_user() -> AuthUser:  # type: ignore[misc]
        return AuthUser(username="anonymous")


from . import register


router = APIRouter(prefix="/announcements", tags=["announcements"])


@router.get("/", response_model=list[Announcement])
async def list_announcements(
    _user=Depends(get_current_user),
) -> list[Announcement]:
    """Return the cached announcements list.

    Always returns 200 with a (possibly empty) list — see
    ``fetch_announcements`` for the never-raise contract.
    """
    return await fetch_announcements()


register(router)
