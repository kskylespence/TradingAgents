"""Live discovery of Ollama / Ollama Cloud models with TTL cache.

`GET {OLLAMA_BASE_URL}/models` returns the OpenAI-shaped list which works
for both local `ollama serve` and Ollama Cloud (`https://ollama.com/v1`).
Successful responses are cached for 5 minutes keyed by `base_url`. On
failure the last-good cached value is returned if one exists, otherwise
an empty list — the catalog endpoint that fans out to this MUST stay
fast and MUST NOT 500 on upstream blips.

Why no exceptions escape: this service is called synchronously from the
catalog handler that builds the provider/model picker. A 5xx or a
hung Ollama would otherwise cascade into a broken settings page; the
contract is "best-effort, fast, never raise". The DEBUG log retains the
underlying error for ops triage.

Two parallel pieces of state per `base_url`:

* ``_cache`` — only updated on **success**. The catalog consumer reads
  from here and uses the last-good list when upstream is briefly down.
* ``_last_attempt`` — updated on **every** fetch attempt (success OR
  failure). The health endpoint reads this to distinguish three cases
  that the catalog's last-good fallback would otherwise conflate:

    - ``"ok"``       — last fetch succeeded (may be empty list if the
                       account genuinely has no models)
    - ``"down"``     — last fetch failed (timeout / 4xx / 5xx)
    - ``"unknown"``  — no fetch attempted yet for this base_url in this
                       process (cold start, never probed)

  This is what stops the health probe from flashing "down" when the
  upstream is actually fine but happens to be returning an empty list,
  and what stops it from confidently reporting "ok" before we've even
  spoken to Ollama.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Literal, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

_TTL_SECONDS = 300.0
_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=5.0)

#: base_url -> (fetched_at_monotonic, models) — ONLY updated on success.
_cache: dict[str, tuple[float, list[str]]] = {}

#: base_url -> (attempted_at_monotonic, success: bool, error_repr | None).
#: Updated on EVERY fetch attempt so the health endpoint can tell
#: "0 models because upstream said so" from "0 models because we have
#: nothing cached and the upstream just failed". Reset by ``_reset_for_tests``.
_last_attempt: dict[str, tuple[float, bool, Optional[str]]] = {}

_lock: Optional[asyncio.Lock] = None

ProbeStatus = Literal["ok", "down", "unknown"]


def _get_lock() -> asyncio.Lock:
    """Lazy-instantiate the lock so it binds to the current loop.

    See `web/backend/CLAUDE.md` — a module-level `asyncio.Lock()` binds
    to whichever loop is current at import time. Under pytest-asyncio
    that loop is dead by the time the test awaits, raising
    `RuntimeError: <Lock> is bound to a different event loop`.
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _resolve_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


async def list_ollama_models() -> list[str]:
    """Return the upstream model id list, cached for `_TTL_SECONDS`.

    On any failure, returns the last-good cached list for the current
    `base_url`, or `[]` if no prior success has been recorded. Never
    raises. Also records the attempt outcome in ``_last_attempt`` so the
    health endpoint can report a meaningful status (see module docstring).
    """
    base_url = _resolve_base_url()
    now = time.monotonic()

    cached = _cache.get(base_url)
    if cached is not None and (now - cached[0]) < _TTL_SECONDS:
        # Cache-hit short-circuit. Note we deliberately DO NOT update
        # ``_last_attempt`` here — the invariant is that ``_cache`` and
        # ``_last_attempt`` are written together in the same critical
        # section below (see lines marked "invariant write"), so a valid
        # cache entry GUARANTEES a corresponding ``"ok"`` entry in
        # ``_last_attempt``. That's why ``last_probe_status()`` can
        # safely report on the latest fetch without each reader also
        # touching the attempt log.
        return cached[1]

    async with _get_lock():
        # Re-check inside the lock — a concurrent task may have populated it.
        cached = _cache.get(base_url)
        if cached is not None and (time.monotonic() - cached[0]) < _TTL_SECONDS:
            # Same cache-hit invariant as the outer check above.
            return cached[1]

        headers: dict[str, str] = {}
        api_key = os.environ.get("OLLAMA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("data", [])
            models = [
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
        except Exception as exc:  # never raise — catalog must stay responsive
            log.warning(
                "ollama_models.fetch_failed",
                extra={"url": url, "error": repr(exc)},
            )
            _last_attempt[base_url] = (time.monotonic(), False, repr(exc))
            if cached is not None:
                return cached[1]
            return []

        # Invariant write: ``_cache`` and ``_last_attempt`` are updated
        # together inside the same lock so any reader that finds a
        # cache entry is guaranteed to also find an ``"ok"`` attempt
        # entry. See the cache-hit short-circuits above.
        _last_attempt[base_url] = (time.monotonic(), True, None)
        _cache[base_url] = (time.monotonic(), models)
        return models


def last_probe_status() -> Tuple[ProbeStatus, Optional[str]]:
    """Return the most recent fetch outcome for the current `base_url`.

    Returns:
        ``("ok", None)``         — last attempt succeeded (model_count
                                   may still be 0 — that's an honest
                                   "upstream said it has no models").
        ``("down", error_repr)`` — last attempt failed.
        ``("unknown", None)``    — no attempt recorded for this
                                   base_url in this process yet.

    Used by the health endpoint to populate ``ollama.status`` honestly,
    instead of inferring "down" from an empty model list (which conflates
    "upstream returned []" with "we've never reached upstream").
    """
    base_url = _resolve_base_url()
    entry = _last_attempt.get(base_url)
    if entry is None:
        return ("unknown", None)
    _, success, error = entry
    return ("ok" if success else "down", error)


def _reset_for_tests() -> None:
    """Test helper — null the lock and clear all cached state.

    Mirrors `event_bus.reset_for_tests()`. Required because pytest-asyncio
    gives each test its own loop; a stale lock from a previous test
    crashes with "bound to a different event loop". Also clears
    ``_last_attempt`` so tests start from the "unknown" state.
    """
    global _lock
    _lock = None
    _cache.clear()
    _last_attempt.clear()


__all__ = [
    "list_ollama_models",
    "last_probe_status",
    "ProbeStatus",
    "_reset_for_tests",
]
