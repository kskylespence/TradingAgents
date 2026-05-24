"""Tests for the ``upstream_warmup`` lifespan hook.

The hook owns two background tasks:

* ``_initial_warmup`` — fired once on startup to pre-resolve DNS/TLS to
  Ollama Cloud so the user's first ``/api/catalog/models`` call doesn't
  pay the full TLS handshake cost (300-800ms).
* ``_refresh_loop`` — runs every 4 minutes (just before the 5-minute
  cache TTL expires) so the user-facing path always hits a warm cache.

Three invariants are pinned here:

1. Startup MUST NOT block on the warmup. A slow / unreachable upstream
   is the exact reason the warmup exists; if it blocked startup, an
   Ollama Cloud outage would prevent the app from booting and Coolify
   would restart-loop the container.
2. The refresh loop calls ``list_ollama_models`` on its configured
   cadence — without this the cache flips to stale every 5 minutes and
   the user-facing call pays the cold-fetch latency on every TTL
   boundary.
3. Shutdown cancels the refresh task cleanly AND closes the shared
   ``upstream_http`` client. Without the cancel we leak a forever-task;
   without the close we leak sockets and pytest-asyncio warns about
   "unclosed transport" between tests.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_warmup_state():
    """Reset the warmup hook's module state around every test.

    The ``_refresh_task`` module global leaks between tests otherwise —
    a previous test's cancelled task can fool ``shutdown_ollama`` into
    thinking nothing needs cancelling on the next test's startup.
    """
    from app.lifespan_hooks import upstream_warmup

    upstream_warmup._refresh_task = None
    upstream_warmup._initial_warmup_task = None
    yield
    # Defensive: if a test forgot to shut down, cancel BOTH tasks so we
    # don't leak them into the next test's loop. Cancelling a task that's
    # already done is a no-op, so this is always safe.
    for attr in ("_initial_warmup_task", "_refresh_task"):
        task = getattr(upstream_warmup, attr, None)
        if task is not None and not task.done():
            task.cancel()
    upstream_warmup._refresh_task = None
    upstream_warmup._initial_warmup_task = None


@pytest.fixture(autouse=True)
def _reset_upstream_http():
    """Reset the shared upstream_http singleton around every test."""
    from app.services import upstream_http

    upstream_http._reset_for_tests()
    yield
    upstream_http._reset_for_tests()


# --------------------------------------------------------------------------- #
# 1. Startup MUST NOT block on a slow upstream                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_warmup_does_not_block_startup(monkeypatch) -> None:
    """A 30-second slow ``list_ollama_models`` MUST NOT block the startup hook.

    The whole point of the warmup is to *hide* upstream latency from the
    boot path. If startup blocked on the warmup, an unreachable Ollama
    Cloud would prevent the app from coming up at all — exactly the
    failure mode we're defending against.
    """
    from app.lifespan_hooks import upstream_warmup

    async def slow_list_models() -> list[str]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(
        "app.services.ollama_models.list_ollama_models", slow_list_models
    )

    started = time.monotonic()
    await upstream_warmup.warmup_ollama(app=None)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"warmup_ollama blocked startup for {elapsed:.2f}s — must be fire-and-forget"
    )

    # Clean up the background tasks the startup hook spawned so they
    # don't leak into the next test.
    await upstream_warmup.shutdown_ollama(app=None)


# --------------------------------------------------------------------------- #
# 2. Refresh loop calls list_ollama_models periodically                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refresh_loop_calls_list_models_periodically(monkeypatch) -> None:
    """The refresh loop should call ``list_ollama_models`` on its interval.

    We crank the interval down to 50ms so the test runs in ~200ms instead
    of 4 minutes, and patch ``list_ollama_models`` to a counter so we can
    observe the call cadence directly.
    """
    from app.lifespan_hooks import upstream_warmup

    call_count = {"n": 0}

    async def counting_list_models() -> list[str]:
        call_count["n"] += 1
        return []

    monkeypatch.setattr(
        "app.services.ollama_models.list_ollama_models", counting_list_models
    )
    # Speed the refresh loop way up so the test stays fast.
    monkeypatch.setattr(upstream_warmup, "_REFRESH_INTERVAL_SECONDS", 0.05)

    await upstream_warmup.warmup_ollama(app=None)

    # Let the refresh loop tick several times. 0.05s interval × 4 ticks
    # = 0.2s; we add a margin for asyncio scheduling jitter.
    await asyncio.sleep(0.25)

    await upstream_warmup.shutdown_ollama(app=None)

    # The initial warmup contributes 1 call; the refresh loop should
    # have added at least 2 more. Total >= 3 keeps the bound robust to
    # scheduling jitter on slow CI runners.
    assert call_count["n"] >= 3, (
        f"refresh loop only fired {call_count['n']} times "
        f"in 0.25s with a 0.05s interval — loop is broken"
    )


# --------------------------------------------------------------------------- #
# 3. Shutdown cancels refresh task + closes shared client                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_shutdown_cancels_refresh_task_and_closes_client(monkeypatch) -> None:
    """Shutdown MUST cancel the refresh task and close the shared client.

    Without the cancel, the refresh task survives the lifespan and ticks
    forever (Python keeps the loop alive until all tasks finish). Without
    the client close, we leak the singleton AsyncClient (and its TLS
    connection pool) which surfaces as "unclosed transport" warnings in
    the next test that uses upstream_http.
    """
    from app.lifespan_hooks import upstream_warmup
    from app.services import upstream_http

    async def trivial_list_models() -> list[str]:
        return []

    monkeypatch.setattr(
        "app.services.ollama_models.list_ollama_models", trivial_list_models
    )
    # Speed the refresh loop so the shutdown path runs against a loop
    # that's actually had a chance to start sleeping.
    monkeypatch.setattr(upstream_warmup, "_REFRESH_INTERVAL_SECONDS", 0.05)

    await upstream_warmup.warmup_ollama(app=None)

    # Force the singleton client to exist so we can prove shutdown
    # closes it (otherwise close_client is a no-op and the assertion is
    # vacuous).
    upstream_http.get_client()
    assert upstream_http._client is not None

    refresh_task = upstream_warmup._refresh_task
    assert refresh_task is not None
    assert not refresh_task.done()

    await upstream_warmup.shutdown_ollama(app=None)

    # Refresh task is now stopped (cancelled or otherwise done).
    assert refresh_task.done(), "refresh task still running after shutdown"

    # Shared client is closed and the singleton is nulled.
    assert upstream_http._client is None, (
        "upstream_http client not closed/nulled by shutdown hook"
    )
