"""Failure-mode tests for `app.services.ollama_models.list_ollama_models`.

The catalog endpoint depends on this service and must stay responsive
even when Ollama is unreachable. The contract:

- After a successful fetch, a later failure (HTTP 5xx, connect error,
  timeout) returns the last-good cached list.
- With no prior success, failures return `[]`. The function never raises.
- An auth failure (401) on the first call is treated the same — empty
  list, not a crash.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

# --------------------------------------------------------------------------- #
# Helpers (mirror test_ollama_models_service.py)                              #
# --------------------------------------------------------------------------- #
# `_reset_ollama_cache` (autouse) is in `conftest.py`. This file keeps its
# own scripted-client helper because it tests sequential success-then-failure
# behavior that requires walking through a multi-step script — beyond the
# scope of the shared `install_fake_httpx_ollama` helper.


def _install_scripted_client(
    monkeypatch: pytest.MonkeyPatch, *script: dict[str, Any]
) -> dict[str, Any]:
    """Install a scripted ``MockTransport`` on the shared ``upstream_http`` client.

    Each entry is either::

        {"json": ..., "status": 200}     # successful response
        {"raise": ConnectError("boom")}   # exception on the transport call

    A ``state["calls"]`` counter tracks invocations.

    Implementation note (v0.2.5+hf.4): ``ollama_models`` now routes
    through ``upstream_http``. We plant the script-driven mock transport
    on that singleton so the retry / circuit-breaker / timeout policy
    all stay exercised against the deterministic responses.
    """
    state: dict[str, Any] = {"calls": 0, "last_url": None, "last_headers": None}
    queue = list(script)

    def _handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        state["last_url"] = str(request.url)
        state["last_headers"] = dict(request.headers)
        if not queue:
            raise AssertionError(
                f"Unexpected extra HTTP call #{state['calls']} to {request.url}"
            )
        step = queue.pop(0)
        if "raise" in step:
            raise step["raise"]
        return httpx.Response(
            step.get("status", 200),
            json=step.get("json"),
            request=request,
        )

    from app.services import ollama_models, upstream_http

    ollama_models._reset_for_tests()
    upstream_http._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        timeout=httpx.Timeout(5.0),
    )
    return state


def _expire_cache() -> None:
    """Force the next call to bypass the TTL cache.

    The service caches by `base_url`; rewriting the cache entry to an
    ancient timestamp is the cleanest way to simulate "TTL elapsed"
    without sleeping or monkeypatching `time.monotonic`.
    """
    from app.services import ollama_models

    for key, (_ts, models) in list(ollama_models._cache.items()):
        ollama_models._cache[key] = (0.0, models)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_after_success_failure_returns_last_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1st call succeeds → 2nd (after TTL) gets 500 → cached list returned."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_scripted_client(
        monkeypatch,
        {"json": {"data": [{"id": "m1"}, {"id": "m2"}]}},
        {"json": {"error": "kaboom"}, "status": 500},
    )

    from app.services.ollama_models import list_ollama_models

    first = await list_ollama_models()
    assert first == ["m1", "m2"]

    _expire_cache()

    second = await list_ollama_models()
    assert second == ["m1", "m2"], "failure after success must return last-good"

    # v0.2.5+hf.4 stale-while-revalidate: the second call returns the
    # cached list IMMEDIATELY and schedules a background refresh that
    # consumes the second scripted entry (the 500 failure). Yield to the
    # loop so the background task runs, then assert both transport
    # calls happened.
    import asyncio
    await asyncio.sleep(0.05)
    assert stats["calls"] == 2


async def test_no_prior_success_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start ConnectError → empty list, no exception bubbles up."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_scripted_client(
        monkeypatch,
        {"raise": httpx.ConnectError("connection refused")},
    )

    from app.services.ollama_models import list_ollama_models

    # Must not raise.
    models = await list_ollama_models()
    assert models == []


async def test_401_returns_empty_initially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start 401 (bad / missing API key) → empty list, no crash."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "wrong-key")

    _install_scripted_client(
        monkeypatch,
        {"json": {"error": "unauthorized"}, "status": 401},
    )

    from app.services.ollama_models import list_ollama_models

    models = await list_ollama_models()
    assert models == []


async def test_failure_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed fetch must NOT overwrite a previously cached good result.

    After cache expiry: fetch fails → return last-good. A third call,
    still within the (just-extended? — no, NOT extended) TTL of the
    original good result, should still see the good cache OR refetch —
    either way, the cached good list must still be the source of truth.

    The behavior we lock in here: a failure is not allowed to write `[]`
    over the previously-good cache entry. This is what protects the
    catalog from flickering empty during transient upstream blips.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_scripted_client(
        monkeypatch,
        {"json": {"data": [{"id": "good"}]}},
        {"raise": httpx.ReadTimeout("slow")},
        {"raise": httpx.ConnectError("down")},
    )

    from app.services import ollama_models
    from app.services.ollama_models import list_ollama_models

    assert await list_ollama_models() == ["good"]

    _expire_cache()
    assert await list_ollama_models() == ["good"]

    # Cache entry should still record the good list, not an empty one
    # injected by the failure path.
    cached_models = ollama_models._cache[
        "https://ollama.example.com/v1"
    ][1]
    assert cached_models == ["good"]

    _expire_cache()
    assert await list_ollama_models() == ["good"]


async def test_drain_in_flight_refreshes_awaits_scheduled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draining must leave no stale-while-revalidate refresh pending.

    ``_schedule_background_refresh`` fires and forgets an ``asyncio.Task``.
    Nothing ever awaited it: ``_reset_for_tests`` only *requests* cancellation
    (``task.cancel()`` throws ``CancelledError`` in at the next suspension
    point, leaving the task in state ``cancelling``), and no shutdown hook
    touched it at all. Either way the loop closes on a live task and asyncio
    logs "Task was destroyed but it is pending!" — noise in tests, and a
    genuinely orphaned HTTP request on a production restart.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_scripted_client(
        monkeypatch,
        {"json": {"data": [{"id": "good"}]}},
        {"json": {"data": [{"id": "fresher"}]}},
    )

    from app.services import ollama_models
    from app.services.ollama_models import list_ollama_models

    assert await list_ollama_models() == ["good"]

    _expire_cache()
    # Stale-serve: returns the cached list immediately and schedules the
    # background refresh this test is about.
    assert await list_ollama_models() == ["good"]

    task = ollama_models._in_flight_refresh["https://ollama.example.com/v1"]
    assert not task.done(), "precondition: the refresh must still be in flight"

    await ollama_models.drain_in_flight_refreshes()

    assert task.done(), "drain must not leave the refresh pending"
    assert ollama_models._in_flight_refresh == {}
    assert stats["calls"] == 2, "the drained refresh should have completed"


async def test_refresh_scheduled_during_drain_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain in progress must stop new refreshes being scheduled behind it.

    ``drain_in_flight_refreshes`` snapshots ``_in_flight_refresh`` and then
    awaits, which yields control. A request handler reaching the
    stale-while-revalidate branch in that window used to add a task the
    snapshot never saw — and the unconditional ``_in_flight_refresh.clear()``
    then discarded the only handle to it. The task kept running untracked
    against a client ``shutdown_ollama`` closes moments later: precisely the
    orphaned-task failure this function exists to prevent, merely narrowed.

    Refusing to schedule while draining closes the window instead of shrinking
    it. Suppression is correct for both callers — process shutdown and test
    teardown — because in each case there is no future in which the refreshed
    value would ever be read.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")

    from app.services import ollama_models

    started = asyncio.Event()

    async def _blocked_fetch(base_url: str) -> list[str]:
        started.set()
        await asyncio.sleep(3600)  # never completes; the drain must cancel it
        return []

    monkeypatch.setattr(ollama_models, "_fetch_now", _blocked_fetch)

    ollama_models._schedule_background_refresh("https://a.example.com/v1")
    await started.wait()

    drain = asyncio.create_task(ollama_models.drain_in_flight_refreshes(timeout=0.05))
    # Yield once so the drain body runs up to its first await.
    await asyncio.sleep(0)

    ollama_models._schedule_background_refresh("https://b.example.com/v1")
    assert "https://b.example.com/v1" not in ollama_models._in_flight_refresh, (
        "a refresh scheduled mid-drain must be suppressed, not orphaned"
    )

    await drain
    assert ollama_models._in_flight_refresh == {}

    # The guard must lift afterwards, or every later refresh is silently dead.
    ollama_models._schedule_background_refresh("https://c.example.com/v1")
    assert "https://c.example.com/v1" in ollama_models._in_flight_refresh
    await ollama_models.drain_in_flight_refreshes(timeout=0.05)
