"""Pre-warm the Ollama HTTP client + periodic catalog refresh.

Part of the v0.2.5+hf.4 deep-resilience pass (Wave 4). Pairs with
``app.services.upstream_http`` (the shared retry+breaker HTTP client)
and ``app.services.ollama_models.list_ollama_models`` (5-min TTL cache).

Why this hook exists
====================
* **Hide cold-start latency.** The first ``list_ollama_models()`` call
  pays full DNS + TLS handshake cost to Ollama Cloud (~300-800ms). Doing
  that during the user's first ``/api/catalog/models`` request makes the
  NewRun page feel sluggish. Pre-warming on startup means the connection
  is already established by the time the user lands.

* **Keep the cache warm.** ``list_ollama_models`` caches successful
  responses for 5 minutes. Without an active refresher, every 5 minutes
  the next user-facing call pays the cold-fetch latency again (the
  stale-while-revalidate path helps on the *second* call but the
  triggering call still serves stale). The 4-minute refresh interval
  intentionally fires *just before* the 5-minute TTL boundary so the
  cache is always fresh when the user-facing read happens.

* **Never block startup.** The warmup is fire-and-forget. An unreachable
  Ollama Cloud (the exact failure mode we're defending against) MUST
  NOT prevent the app from booting — otherwise a single upstream blip
  would Coolify-restart-loop the container. The warmup task uses
  ``asyncio.wait_for(..., timeout=20)`` as a defense-in-depth bound and
  catches every exception.

What it does NOT own
====================
* Catalog fetching itself — that's ``ollama_models.list_ollama_models``.
  This hook is purely a *driver* that calls that function on a schedule.
* The shared client lifecycle — ``upstream_http.get_client`` /
  ``close_client`` own that. The shutdown half of this hook just calls
  ``close_client`` at teardown.

Test seams
==========
* ``_REFRESH_INTERVAL_SECONDS`` is module-level so tests can monkeypatch
  it down to a sub-second value and observe several refresh cycles
  without waiting four minutes.
* ``_refresh_task`` is module-level so tests can assert it gets
  cancelled by ``shutdown_ollama``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI

from . import on_shutdown, on_startup

log = logging.getLogger(__name__)


# 4 minutes — fires just before the 5-minute ``_TTL_SECONDS`` boundary
# in ``ollama_models``. Module-level (not a constant inside the loop) so
# tests can monkeypatch it.
_REFRESH_INTERVAL_SECONDS: float = 240.0

# Defense-in-depth wall-clock cap on the initial warmup. The warmup is
# already wrapped in a bare ``except Exception`` so a hung upstream
# can't take down startup — this is just belt-and-braces so even a
# pathological 60-minute hang in ``list_ollama_models`` doesn't keep the
# warmup task alive indefinitely.
_INITIAL_WARMUP_TIMEOUT_SECONDS: float = 20.0

# Module-level handles on the running tasks. Held so ``shutdown_ollama``
# can cancel them cleanly during FastAPI teardown. Both need tracking:
# the initial warmup can outlive the refresh loop if the upstream is
# slow, and without an explicit cancel it becomes an orphan task that
# pytest-asyncio test isolation cannot reliably clean up — observed as
# cross-test event-loop pollution.
_refresh_task: Optional[asyncio.Task] = None
_initial_warmup_task: Optional[asyncio.Task] = None


@on_startup
async def warmup_ollama(app: FastAPI) -> None:
    """Spawn the initial warmup + the periodic refresh loop.

    Both are fire-and-forget ``asyncio.create_task`` calls — this
    function returns within microseconds even when ``list_ollama_models``
    would block for the full 20-second warmup timeout. Startup latency
    is unaffected by upstream health.
    """
    global _refresh_task, _initial_warmup_task
    _initial_warmup_task = asyncio.create_task(
        _initial_warmup(), name="ollama-warmup"
    )
    _refresh_task = asyncio.create_task(_refresh_loop(), name="ollama-refresh")
    log.info(
        "upstream_warmup.tasks_started",
        extra={"refresh_interval_seconds": _REFRESH_INTERVAL_SECONDS},
    )


async def _initial_warmup() -> None:
    """Fire one ``list_ollama_models()`` call to pre-resolve DNS+TLS.

    Catches every exception — the warmup is best-effort. The regular
    30-second health polls + the 4-minute refresh loop will retry on
    their own cadences if this fails.
    """
    # Lazy import: keeps the lifespan_hooks package import-time free of
    # the (transitively heavy) ollama_models import chain. The autoload
    # in ``app/lifespan_hooks/__init__.py`` runs at backend boot, well
    # before any request lands, so this lazy hop costs nothing in
    # steady state.
    from app.services.ollama_models import list_ollama_models

    try:
        await asyncio.wait_for(
            list_ollama_models(), timeout=_INITIAL_WARMUP_TIMEOUT_SECONDS
        )
        log.info("upstream_warmup.initial_ok")
    except asyncio.CancelledError:
        # If the lifespan is being torn down before warmup finishes,
        # let CancelledError propagate so the task transitions to
        # cancelled state cleanly.
        raise
    except Exception:  # noqa: BLE001 — best-effort warmup; never raise
        log.warning("upstream_warmup.initial_failed", exc_info=True)


async def _refresh_loop() -> None:
    """Sleep then refresh, forever. Cancelled by ``shutdown_ollama``.

    Order matters: sleep FIRST, then fetch. The initial warmup already
    populated the cache (or tried to); the loop's job is to keep it
    fresh, not to do the cold fetch a second time at t=0.

    ``CancelledError`` re-raises so the task transitions to cancelled
    state — anything else (network exceptions, parse errors, etc.) is
    logged and swallowed so the loop survives a transient.
    """
    from app.services.ollama_models import list_ollama_models

    while True:
        try:
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)
            await list_ollama_models()
            log.debug("upstream_warmup.refresh_ok")
        except asyncio.CancelledError:
            log.info("upstream_warmup.refresh_cancelled")
            raise
        except Exception:  # noqa: BLE001 — loop must survive transients
            log.warning("upstream_warmup.refresh_failed", exc_info=True)


@on_shutdown
async def shutdown_ollama(app: FastAPI) -> None:
    """Cancel the refresh loop, await it, then close the shared client.

    Three-step shutdown so we (a) stop scheduling new requests, (b) wait
    for the in-flight one to finish or surface its CancelledError, and
    (c) close the underlying TLS pool. Skipping any step leaks resources
    — the refresh task would keep running, or the AsyncClient sockets
    would stay open into the next process state.
    """
    global _refresh_task, _initial_warmup_task

    # Cancel the warmup task first — it may still be in flight against a
    # slow / unreachable upstream. Without the explicit cancel an orphan
    # task bleeds into the next pytest-asyncio loop (event-loop pollution).
    if _initial_warmup_task is not None and not _initial_warmup_task.done():
        _initial_warmup_task.cancel()
        try:
            await _initial_warmup_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("upstream_warmup.initial_shutdown_failed")
    _initial_warmup_task = None

    if _refresh_task is not None and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            # Expected — the .cancel() above unwinds the loop's sleep.
            pass
        except Exception:  # noqa: BLE001 — log but proceed to client close
            log.exception("upstream_warmup.refresh_shutdown_failed")
    _refresh_task = None

    # Lazy import so a broken ``upstream_http`` doesn't prevent the rest
    # of shutdown from running. ``close_client`` is itself safe to call
    # even if the singleton was never constructed.
    from app.services import upstream_http

    await upstream_http.close_client()
    log.info("upstream_warmup.shutdown_complete")


__all__ = ["shutdown_ollama", "warmup_ollama"]
