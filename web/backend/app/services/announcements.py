"""Announcements proxy service.

Fetches the operator-broadcast announcements feed from the public
``api.tauric.ai`` endpoint and caches results in-process for 60 seconds.

Error posture (mirrors ``cli/announcements.py``):
- 1 second connect+read timeout — never block the UI on a slow upstream.
- ANY exception (timeout, network, non-2xx, malformed JSON, schema
  validation failure) is logged at WARNING and swallowed; callers get
  an empty list back so the frontend always renders.
- Successful payloads populate the cache; failed fetches do NOT poison
  the cache (a stale-but-valid payload is preferred over a transient
  upstream blip propagating empty everywhere for 60s).

Concurrency: a single asyncio.Lock guards the cache so concurrent
in-flight requests don't dogpile the upstream when the cache is cold or
expired — only one fetch happens per TTL window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TypedDict

import httpx
from pydantic import TypeAdapter, ValidationError

from app.schemas import Announcement

# Hardcoded upstream URL — the CLI uses cli/config.py's
# `announcements_url`, but the backend has no equivalent config knob and
# the team owns no edit access to `app/config.py`. If we ever need a
# per-env override, add an ANNOUNCEMENTS_URL field to Settings; until
# then, this constant is the single source of truth.
ANNOUNCEMENTS_URL = "https://api.tauric.ai/v1/announcements"

# Upstream timeout: deliberately tight. The CLI uses 1s; matching it
# keeps the contract identical (slow upstream → empty list immediately,
# never block the dashboard render).
REQUEST_TIMEOUT_SECONDS = 1.0

# How long a successful fetch stays valid in-process. 60s is enough to
# absorb the typical dashboard refresh cadence without hammering the
# upstream from every page load.
CACHE_TTL_SECONDS = 60.0


log = logging.getLogger(__name__)


_ANNOUNCEMENT_LIST_ADAPTER: TypeAdapter[list[Announcement]] = TypeAdapter(
    list[Announcement]
)


class _CacheEntry(TypedDict):
    data: list[Announcement]
    fetched_at: float


# Module-level cache + lock. Tests reset via _reset_cache_for_tests().
_cache: _CacheEntry | None = None
_cache_lock: asyncio.Lock = asyncio.Lock()


def _now() -> float:
    """Indirection seam so tests can freeze time without monkeypatching time itself."""
    return time.monotonic()


def _cache_is_fresh(entry: _CacheEntry | None) -> bool:
    if entry is None:
        return False
    return (_now() - entry["fetched_at"]) < CACHE_TTL_SECONDS


async def _fetch_from_upstream() -> list[Announcement]:
    """Single attempt at the upstream. Raises on any failure — caller decides what to do."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.get(ANNOUNCEMENTS_URL)
    response.raise_for_status()
    payload = response.json()
    # The endpoint MAY wrap its list in a top-level `announcements` key
    # (mirrors cli/announcements.py's `data.get("announcements", ...)`)
    # or return a bare list. Support both shapes.
    if isinstance(payload, dict):
        items = payload.get("announcements", [])
    else:
        items = payload
    return _ANNOUNCEMENT_LIST_ADAPTER.validate_python(items)


async def fetch_announcements() -> list[Announcement]:
    """Return the cached or freshly-fetched announcement list.

    Never raises. On any error (timeout, network, malformed JSON, schema
    mismatch), logs at WARNING and returns ``[]``. A stale cache entry is
    NOT served on error — the contract is "always return something
    parseable" not "always return the freshest possible thing."
    """
    global _cache

    # Fast path: lock-free read of a fresh cache entry. Python's GIL
    # makes a single attribute read atomic, and the worst case here is
    # one extra upstream fetch on a TTL boundary — acceptable.
    snapshot = _cache
    if _cache_is_fresh(snapshot):
        return list(snapshot["data"])  # type: ignore[index]

    async with _cache_lock:
        # Re-check under the lock — another coroutine may have refreshed
        # while we were waiting for the lock.
        if _cache_is_fresh(_cache):
            return list(_cache["data"])  # type: ignore[index]

        try:
            data = await _fetch_from_upstream()
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            # httpx.HTTPError covers TimeoutException, ConnectError,
            # HTTPStatusError (from raise_for_status). ValueError covers
            # json.JSONDecodeError. ValidationError covers schema misses.
            log.warning(
                "announcements.fetch_failed",
                extra={"error_type": type(exc).__name__, "error": str(exc)},
            )
            return []
        except Exception as exc:  # pragma: no cover - belt-and-suspenders
            # Catch-all: announcements MUST NOT take down the dashboard.
            log.warning(
                "announcements.unexpected_failure",
                extra={"error_type": type(exc).__name__, "error": str(exc)},
            )
            return []

        _cache = {"data": data, "fetched_at": _now()}
        return list(data)


def _reset_cache_for_tests() -> None:
    """Test-only hook. Wipes the module-level cache between cases."""
    global _cache
    _cache = None


__all__ = [
    "ANNOUNCEMENTS_URL",
    "REQUEST_TIMEOUT_SECONDS",
    "CACHE_TTL_SECONDS",
    "fetch_announcements",
]
