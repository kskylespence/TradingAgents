"""Tests for ``app.services.upstream_http`` — the shared resilient HTTP client.

This module is the load-bearing piece of the v0.2.5+hf.4 resilience pass:
every Ollama call (the catalog probe path) is routed through it, and its
correct behavior is what stops the "Ollama upstream is unreachable" red
alert from flapping on a single transient TCP RTT spike.

Eight behaviors are pinned here:

1. Retry on transient connect errors (the original ConnectTimeout('')
   flap symptom).
2. Give up after the configured attempt budget — never retry forever.
3. Honor a ``Retry-After: <integer-seconds>`` header verbatim.
4. Honor a ``Retry-After: <HTTP-date>`` header (RFC 7231 allows both).
5. The circuit breaker opens after the configured consecutive-failure
   threshold so we stop hammering an exhausted upstream
   (defends against Ollama Cloud #15419 503-burst patterns).
6. The breaker half-opens after the recovery window so the upstream
   gets a single trial probe before the breaker re-closes — without
   the half-open transition, a recovered upstream stays "down" until
   process restart.
7. The httpx.AsyncClient is a singleton — connection reuse is what
   makes the steady-state catalog probe fast (~150ms vs ~600ms).
8. ``close_client()`` cleanly aexits the singleton so we don't leak
   sockets on app shutdown.

The tests use ``httpx.MockTransport`` (the documented httpx-native test
seam) rather than monkey-patching ``httpx.AsyncClient`` itself; this
exercises the real client wiring + the real circuit breaker + the real
tenacity retry loop, just with deterministic transport responses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from time import monotonic

import httpx
import pytest
from circuitbreaker import CircuitBreakerError

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the upstream_http singleton + breaker around every test.

    Without this, breaker state (failure_count, _opened) leaks between
    tests and the order of test execution affects results — pytest's
    test isolation contract demands deterministic per-test state.
    """
    from app.services import upstream_http

    upstream_http._reset_for_tests()
    yield
    upstream_http._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_tenacity_wait(monkeypatch):
    """Replace the exponential-jitter wait with no-wait for fast tests.

    Production code uses ``wait_exponential_jitter(initial=0.5, max=8.0)``;
    three retries would take 0.5+1.5+~4 = ~6s wall, which is too slow
    for unit tests. The Retry-After sleep (a separate code path inside
    the call site) is NOT affected by this fixture, so test 3 + test 4
    still exercise that behavior end-to-end.
    """
    from app.services import upstream_http
    from tenacity import wait_none

    monkeypatch.setattr(upstream_http, "_WAIT_STRATEGY", wait_none())


def _install_mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Replace the upstream_http singleton with one wired to a mock transport.

    The handler receives an ``httpx.Request`` and either:
    * returns an ``httpx.Response`` (success path), or
    * raises an exception (transient-error path — httpx.ConnectTimeout etc.).
    """
    from app.services import upstream_http

    transport = httpx.MockTransport(handler)
    upstream_http._client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(5.0),
    )


# --------------------------------------------------------------------------- #
# 1. Retry on transient connect errors                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_retries_on_connect_timeout() -> None:
    """Two ConnectTimeouts then 200 → request returns the 200.

    This is the flap symptom from the field: a single TCP RTT spike used
    to surface to the user as a red alert. With retry, the user never
    sees a transient.
    """
    from app.services import upstream_http

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("simulated TCP timeout")
        return httpx.Response(200, json={"data": [{"id": "ok"}]})

    _install_mock_transport(handler)

    resp = await upstream_http.request(
        "GET", "https://x/v1/models", max_attempts=3, max_total_seconds=10
    )

    assert resp.status_code == 200
    assert resp.json() == {"data": [{"id": "ok"}]}
    assert len(calls) == 3  # 2 failed + 1 succeeded


# --------------------------------------------------------------------------- #
# 2. Give up after attempt budget                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_gives_up_after_max_attempts() -> None:
    """Three ConnectTimeouts with max_attempts=3 → original exception raised.

    Tenacity is configured with ``reraise=True`` so the caller sees the
    actual ``httpx.ConnectTimeout`` (not a ``RetryError`` wrapper) —
    callers in ollama_models.py rely on this to record the right
    ``repr(exc)`` in ``_last_attempts``.
    """
    from app.services import upstream_http

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("always fails")

    _install_mock_transport(handler)

    with pytest.raises(httpx.ConnectTimeout):
        await upstream_http.request(
            "GET", "https://x/v1/models", max_attempts=3, max_total_seconds=10
        )

    assert len(calls) == 3  # exactly the attempt budget — no over-retry


# --------------------------------------------------------------------------- #
# 3. Honor Retry-After (integer seconds)                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_honors_retry_after_seconds(monkeypatch) -> None:
    """``Retry-After: 1`` → asyncio.sleep called with ~1.0 before retry.

    The header tells us how long the upstream wants us to back off.
    Ignoring it (the pre-PR behavior) amplifies failures during a 503
    burst — Ollama Cloud #15419.
    """
    from app.services import upstream_http

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(upstream_http, "_async_sleep", fake_sleep)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "1"}, json={"error": "slow down"}
            )
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(handler)

    resp = await upstream_http.request(
        "GET", "https://x/v1/models", max_attempts=3, max_total_seconds=10
    )

    assert resp.status_code == 200
    assert len(calls) == 2  # 429 then 200
    # Exactly one Retry-After-honored sleep, for ~1.0s.
    assert 1 in [int(s) for s in slept], f"expected sleep(1), got {slept}"


# --------------------------------------------------------------------------- #
# 4. Honor Retry-After (HTTP-date format)                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_request_honors_retry_after_http_date(monkeypatch) -> None:
    """``Retry-After: <HTTP-date>`` → asyncio.sleep called with the date delta.

    RFC 7231 §7.1.3 allows EITHER an integer or an HTTP-date for the
    Retry-After header value. Ollama Cloud has been observed sending
    both; we must honor either form or the wait gets ignored.
    """
    from app.services import upstream_http

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(upstream_http, "_async_sleep", fake_sleep)

    future = datetime.now(timezone.utc) + timedelta(seconds=3)
    http_date = format_datetime(future, usegmt=True)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": http_date},
                json={"error": "unavailable"},
            )
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(handler)

    resp = await upstream_http.request(
        "GET", "https://x/v1/models", max_attempts=3, max_total_seconds=10
    )

    assert resp.status_code == 200
    assert len(calls) == 2
    # The date is ~3 seconds in the future; allow a 1s slop window for
    # parse + the moment between header-construction and sleep-call.
    assert any(
        2.0 <= s <= 4.0 for s in slept
    ), f"expected sleep(~3s) from HTTP-date, got {slept}"


# --------------------------------------------------------------------------- #
# 5. Circuit opens after threshold failures                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_circuit_opens_after_5_failures() -> None:
    """Five consecutive ConnectTimeouts (one per request, max_attempts=1) →
    sixth request raises CircuitBreakerError without hitting the transport.

    With max_attempts=1, each request consumes exactly one breaker
    failure on its way through. After threshold=5, the breaker opens
    and the sixth call short-circuits before reaching the transport.
    Production threshold is 5 (per the plan); this test pins it.
    """
    from app.services import upstream_http

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("always fails")

    _install_mock_transport(handler)

    # 5 requests, each raising — each counts as one breaker failure.
    for _ in range(5):
        with pytest.raises(httpx.ConnectTimeout):
            await upstream_http.request(
                "GET", "https://x/v1/models",
                max_attempts=1, max_total_seconds=10,
            )

    assert len(calls) == 5
    assert upstream_http.circuit_state() == "open"

    # 6th request — circuit is open; CircuitBreakerError raised, no transport call.
    with pytest.raises(CircuitBreakerError):
        await upstream_http.request(
            "GET", "https://x/v1/models",
            max_attempts=1, max_total_seconds=10,
        )

    assert len(calls) == 5  # transport NOT hit on the 6th attempt


# --------------------------------------------------------------------------- #
# 6. Circuit half-opens after cooldown                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_circuit_half_opens_after_cooldown() -> None:
    """After recovery_timeout, the next call is a half-open trial.

    On half-open: a success closes the circuit; a failure re-opens it.
    Without this transition a recovered upstream stays "down" until
    process restart.
    """
    from app.services import upstream_http

    calls: list[int] = []
    should_fail = [True] * 5  # consume to open

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if should_fail and should_fail.pop(0):
            raise httpx.ConnectTimeout("transient")
        return httpx.Response(200, json={"ok": True})

    _install_mock_transport(handler)

    # Drive 5 failures to open.
    for _ in range(5):
        with pytest.raises(httpx.ConnectTimeout):
            await upstream_http.request(
                "GET", "https://x/v1/models",
                max_attempts=1, max_total_seconds=10,
            )

    assert upstream_http.circuit_state() == "open"

    # Force the cooldown window to expire — backdate the breaker's
    # ``_opened`` (a time.monotonic() timestamp) to N+1 seconds before
    # ``now``, where N is the recovery_timeout. This is the documented
    # internal layout of the ``circuitbreaker`` package as of 2.1.x.
    breaker = upstream_http._breaker
    breaker._opened = monotonic() - (breaker._recovery_timeout + 1)
    assert upstream_http.circuit_state() == "half_open"

    # Next request is the half-open trial — it succeeds (no should_fail
    # entries left), so the breaker should close.
    resp = await upstream_http.request(
        "GET", "https://x/v1/models",
        max_attempts=1, max_total_seconds=10,
    )
    assert resp.status_code == 200
    assert upstream_http.circuit_state() == "closed"


# --------------------------------------------------------------------------- #
# 7. Singleton client reused                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_singleton_client_reused() -> None:
    """Multiple ``get_client()`` calls return the same AsyncClient instance.

    This is what amortizes the TLS handshake — each fresh client costs
    300-800ms on the first request. With reuse, only the first request
    pays that cost.
    """
    from app.services import upstream_http

    c1 = upstream_http.get_client()
    c2 = upstream_http.get_client()
    c3 = upstream_http.get_client()
    assert c1 is c2 is c3


# --------------------------------------------------------------------------- #
# 8. close_client cleanly aexits                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_close_client_aexits_cleanly() -> None:
    """``close_client()`` closes the singleton + nulls it so the next
    ``get_client()`` constructs a fresh one."""
    from app.services import upstream_http

    c1 = upstream_http.get_client()
    assert not c1.is_closed

    await upstream_http.close_client()
    assert c1.is_closed
    assert upstream_http._client is None

    # Lazy re-construction works after close.
    c2 = upstream_http.get_client()
    assert c2 is not c1
    assert not c2.is_closed

    await upstream_http.close_client()


# --------------------------------------------------------------------------- #
# 9. circuit_state public helper                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_circuit_state_public_helper() -> None:
    """``circuit_state()`` returns one of 'closed' | 'open' | 'half_open'.

    The frontend reads this via /api/health to render the recovering
    pill / cool-down countdown / red alert states.
    """
    from app.services import upstream_http

    assert upstream_http.circuit_state() == "closed"  # fresh process
