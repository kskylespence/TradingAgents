"""Tests for the resilience behaviours added to ``ollama_models`` in
v0.2.5+hf.4.

The pre-existing ``test_ollama_models_service.py`` pins the catalog
parsing + cache contract; this file pins the new behaviour that depends
on the shared ``upstream_http`` module:

* The catalog request goes through ``upstream_http.request`` (which
  carries the retry+breaker+pool layer), NOT a per-call
  ``httpx.AsyncClient``.
* ``last_probe_status()`` has hysteresis — a single failure with two
  prior successes still reports ``"ok"``. This is the load-bearing
  change that stops the user-visible alert from flapping.
* On cache expiry, ``list_ollama_models()`` returns the stale list
  immediately and refreshes in the background (stale-while-revalidate)
  — no user-facing call ever blocks on a cold fetch after the first.
* When the circuit breaker is OPEN, ``list_ollama_models()`` falls back
  to last-good cache instead of raising; ``last_probe_status()`` still
  honestly reports the "down" / "open" state via the next path.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_upstream_http() -> Any:
    """Reset both ``ollama_models`` and ``upstream_http`` per test.

    The autouse ``_reset_ollama_cache`` in conftest.py resets only
    ``ollama_models`` state; after the v0.2.5+hf.4 refactor the
    breaker + singleton client in ``upstream_http`` must also be
    reset or breaker state leaks between tests.
    """
    from app.services import upstream_http

    upstream_http._reset_for_tests()
    yield
    upstream_http._reset_for_tests()


def _install_mock_upstream(
    handler,
) -> None:
    """Wire ``upstream_http``'s singleton client to a mock transport."""
    from app.services import upstream_http

    transport = httpx.MockTransport(handler)
    upstream_http._client = httpx.AsyncClient(
        transport=transport, timeout=httpx.Timeout(5.0)
    )


# --------------------------------------------------------------------------- #
# Path-through assertions                                                     #
# --------------------------------------------------------------------------- #


async def test_list_ollama_models_uses_shared_client(monkeypatch) -> None:
    """The service routes through ``upstream_http.request`` — not a
    per-call ``httpx.AsyncClient(...)`` block.

    This is what gives us connection pooling + retry + breaker for the
    catalog/health probe path. If a future refactor accidentally
    bypasses the shared client (e.g. someone re-adds an inline httpx
    call), this test fails loud.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models, upstream_http

    called = {"count": 0, "url": None}

    async def fake_request(method, url, *, headers=None, json_body=None,
                           max_attempts=3, max_total_seconds=25.0):
        called["count"] += 1
        called["url"] = url
        # ``raise_for_status()`` requires ``request`` to be set on the
        # response — synthesise a matching request so the response is
        # well-formed for downstream consumers.
        return httpx.Response(
            200,
            json={"data": [{"id": "model-x"}]},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(upstream_http, "request", fake_request)

    result = await ollama_models.list_ollama_models()

    assert called["count"] == 1
    assert called["url"] == "https://ollama.test/v1/models"
    assert result == ["model-x"]


# --------------------------------------------------------------------------- #
# Hysteresis                                                                  #
# --------------------------------------------------------------------------- #


async def test_last_probe_status_hysteresis_single_failure_stays_ok(
    monkeypatch,
) -> None:
    """A single recent failure with two prior successes stays "ok".

    This is THE load-bearing user-visible change. Before hysteresis, a
    single 2-second TCP RTT spike during the health-poll cycle flipped
    the user's alert red. After: two failures of the last three are
    needed to flip — protects against single transients.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models

    base_url = "https://ollama.test/v1"

    # Drive the per-base-URL attempt log directly — we're testing the
    # status interpreter, not the request flow.
    log = ollama_models._last_attempts.setdefault(
        base_url, deque(maxlen=3)
    )
    import time
    log.append((time.monotonic(), True, None))   # oldest: ok
    log.append((time.monotonic(), True, None))   # ok
    log.append((time.monotonic(), False, "ConnectTimeout('')"))  # newest: fail

    status, error = ollama_models.last_probe_status()
    assert status == "ok", (
        f"hysteresis violated: a single failure with two prior successes "
        f"should stay 'ok', but got status={status!r} error={error!r}"
    )


async def test_last_probe_status_two_of_three_failures_flips_down(
    monkeypatch,
) -> None:
    """Two failures of the last three flips to "down" — a genuine outage
    pattern, not a transient. The error from the most recent failure is
    surfaced so operators can triage."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models

    base_url = "https://ollama.test/v1"
    log = ollama_models._last_attempts.setdefault(base_url, deque(maxlen=3))
    import time
    log.append((time.monotonic(), True, None))
    log.append((time.monotonic(), False, "ConnectTimeout('first')"))
    log.append((time.monotonic(), False, "ConnectTimeout('second')"))

    status, error = ollama_models.last_probe_status()
    assert status == "down"
    # Surface the LATEST error — that's the most actionable one.
    assert error == "ConnectTimeout('second')"


async def test_last_probe_status_three_failures_flips_down(monkeypatch) -> None:
    """Three failures of the last three is the strongest "down" signal."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models

    base_url = "https://ollama.test/v1"
    log = ollama_models._last_attempts.setdefault(base_url, deque(maxlen=3))
    import time
    log.append((time.monotonic(), False, "err1"))
    log.append((time.monotonic(), False, "err2"))
    log.append((time.monotonic(), False, "err3"))

    status, error = ollama_models.last_probe_status()
    assert status == "down"
    assert error == "err3"


async def test_last_probe_status_unknown_when_no_attempts(monkeypatch) -> None:
    """No attempts recorded for this base_url → 'unknown' (not 'down').

    Distinguishes "we've never tried" from "we tried and failed" — the
    cold-start case shouldn't surface as if upstream is broken.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models

    status, error = ollama_models.last_probe_status()
    assert status == "unknown"
    assert error is None


# --------------------------------------------------------------------------- #
# Stale-while-revalidate                                                      #
# --------------------------------------------------------------------------- #


async def test_list_ollama_models_serves_stale_while_revalidating(
    monkeypatch,
) -> None:
    """Cache expired → return stale list immediately + refresh in background.

    Without this, every 5th minute the user-facing call to
    ``/api/catalog/models?provider=ollama`` pays the full cold-fetch
    latency (~600ms). With stale-while-revalidate, the page stays snappy
    while the background refresh updates the cache.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models, upstream_http

    # Seed the cache with a stale (>5 min old) entry.
    import time
    base_url = "https://ollama.test/v1"
    ollama_models._cache[base_url] = (
        time.monotonic() - (ollama_models._TTL_SECONDS + 60),
        ["stale-model-1", "stale-model-2"],
    )
    ollama_models._last_attempts[base_url] = deque(
        [(time.monotonic() - 600, True, None)], maxlen=3
    )

    # Slow the upstream so we can distinguish "blocking" from "background".
    fresh_called = asyncio.Event()
    block_release = asyncio.Event()

    async def slow_fake(method, url, *, headers=None, json_body=None,
                        max_attempts=3, max_total_seconds=25.0):
        fresh_called.set()
        await block_release.wait()
        return httpx.Response(
            200,
            json={"data": [{"id": "fresh-1"}]},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(upstream_http, "request", slow_fake)

    # The call MUST return the stale value without waiting for the
    # in-flight fresh fetch.
    result = await asyncio.wait_for(
        ollama_models.list_ollama_models(), timeout=2.0
    )
    assert result == ["stale-model-1", "stale-model-2"], (
        f"expected stale list immediately, got {result}"
    )
    # ``asyncio.create_task`` schedules but doesn't run until the loop
    # yields. Give the background task a tick to enter ``slow_fake``.
    await asyncio.sleep(0)
    assert fresh_called.is_set(), "background refresh task was not started"

    # Let the background task complete cleanly so the test's event loop
    # closes without orphaned tasks.
    block_release.set()
    await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- #
# Circuit-open fallback                                                       #
# --------------------------------------------------------------------------- #


async def test_list_ollama_models_circuit_open_serves_cache(monkeypatch) -> None:
    """When the breaker is OPEN, the service returns the last-good cache,
    not the exception. ``last_probe_status()`` still honestly reports the
    failure via the attempt log so the health endpoint stays accurate.

    We seed a STALE cache so ``list_ollama_models`` triggers a
    background refresh (which is where the breaker-error fallback path
    runs); the synchronous call returns the stale list immediately, and
    after we yield to the loop the background task records the failure.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models, upstream_http
    from circuitbreaker import CircuitBreakerError

    import time
    base_url = "https://ollama.test/v1"
    ollama_models._cache[base_url] = (
        time.monotonic() - (ollama_models._TTL_SECONDS + 60),
        ["cached-1", "cached-2"],
    )

    async def always_breaker_error(method, url, *, headers=None,
                                    json_body=None, max_attempts=3,
                                    max_total_seconds=25.0):
        raise CircuitBreakerError(upstream_http._breaker)

    monkeypatch.setattr(upstream_http, "request", always_breaker_error)

    result = await ollama_models.list_ollama_models()
    # Stale cached list is returned — no exception.
    assert result == ["cached-1", "cached-2"]

    # Yield so the background refresh task runs and records its outcome.
    await asyncio.sleep(0.05)

    # The attempt log records the breaker failure so the health endpoint
    # can surface "circuit open" honestly.
    log = ollama_models._last_attempts.get(base_url)
    assert log is not None
    last_at, last_ok, last_err = log[-1]
    assert last_ok is False
    assert "CircuitBreakerError" in (last_err or "")


# --------------------------------------------------------------------------- #
# Recent-attempts public read for the health endpoint                         #
# --------------------------------------------------------------------------- #


async def test_recent_attempts_returns_last_three(monkeypatch) -> None:
    """``recent_attempts()`` exposes the rolling-3 attempt log to the
    health endpoint so the UI can render last-N outcomes."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.test/v1")

    from app.services import ollama_models

    base_url = "https://ollama.test/v1"
    log = ollama_models._last_attempts.setdefault(base_url, deque(maxlen=3))
    import time
    log.append((time.monotonic(), True, None))
    log.append((time.monotonic(), False, "ConnectTimeout('x')"))
    log.append((time.monotonic(), True, None))

    out = ollama_models.recent_attempts()
    assert isinstance(out, list)
    assert len(out) == 3
    # Newest last; each entry has at/ok/error fields.
    assert out[-1]["ok"] is True
    assert out[-2]["ok"] is False
    assert out[-2]["error"] == "ConnectTimeout('x')"
    # Each entry has an ISO timestamp.
    for entry in out:
        assert "at" in entry
