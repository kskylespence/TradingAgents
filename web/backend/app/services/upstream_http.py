"""Shared resilient HTTP client for outbound calls to flaky upstreams.

Layer 0 of the v0.2.5+hf.4 resilience pass. Owns every outbound HTTP
call to Ollama Cloud (the catalog/health probe path; pre-flight model
probe; future data-vendor calls). The four-layer defense added in
0.2.5+hf.3 only protected the *run* pipeline (the LangChain-driven
LLM calls). This module is what stops the user-visible
"ConnectTimeout('')" red alert from flapping on a single TCP RTT spike.

What it owns
============
* **One module-singleton ``httpx.AsyncClient``** with HTTP/2,
  ``Limits()``, and a sane ``Timeout(connect=10, read=15, write=10,
  pool=10)``. The singleton amortizes TLS handshake cost — first
  request ~600ms, every subsequent request ~150ms. Connection reuse
  is the single biggest steady-state UX improvement.

* **Tenacity retry** with ``wait_exponential_jitter`` + bounded
  ``stop_after_attempt`` AND ``stop_after_delay``. Retries on
  transient transport errors AND on 429/5xx responses (the documented
  Ollama Cloud failure modes — see `ollama/ollama#15419`,
  `#15910`, `#13770`). Tenacity is configured with ``reraise=True``
  so callers see the actual ``httpx.ConnectTimeout`` /
  ``httpx.HTTPError``, not a ``RetryError`` wrapper — needed because
  ``ollama_models.py`` records ``repr(exc)`` in the attempt log.

* **A circuit breaker** (the ``circuitbreaker`` package's
  ``CircuitBreaker`` class). Opens after 5 consecutive failures,
  stays open for 30s, then half-opens for one trial probe before
  closing. Without this, a sustained 503 burst from Ollama Cloud
  cascades into "every poll cycle hammers the upstream" which makes
  the outage worse — the breaker turns "be a good citizen" from a
  comment into code.

* **``Retry-After`` header honouring** on 429/503 responses. RFC 7231
  §7.1.3 allows EITHER an integer second count or an HTTP-date; both
  are parsed and converted to a sleep before the next retry. Without
  this, we ignore Ollama Cloud's own backpressure signal and amplify
  the failure.

* **Structured logging** at every meaningful transition — retry
  attempt, Retry-After honored, circuit opened/closed — using the
  ``upstream_http.<event>`` namespace so operators can grep for one
  failure mode at a time during production triage.

What it does NOT own
====================
* The LangChain/OpenAI run-time call path
  (``tradingagents.llm_clients.openai_client.NormalizedChatOpenAI``).
  That has its own retry layer (``max_retries=5``) and a much longer
  read budget (120-300s per model). Layering this module's circuit
  breaker on top would cascade with the SDK's own retries and
  amplify failures. Each upstream call path gets exactly one
  retry/breaker layer; this is "circuit breaker per failure domain"
  from the SRE playbook.

Test seams
==========
* ``_client`` and ``_breaker`` are module-level singletons; tests
  replace them via ``_reset_for_tests()`` + direct assignment.
* ``_WAIT_STRATEGY`` is module-level so tests can monkeypatch it to
  ``wait_none()`` for fast execution while the Retry-After sleep
  path (which uses ``_async_sleep``) stays exercised.
* ``_async_sleep`` is module-level so tests can monkeypatch it to
  record the durations passed (and skip real wall-clock sleeping).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from circuitbreaker import CircuitBreaker
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

# Tuned for the catalog/health probe path. The LLM run path uses its own
# longer-read client inside ``openai_client.py``. The 10s connect timeout
# brings this in line with the run pipeline (the asymmetry that caused
# the flap was 2.0s connect here vs 10.0s connect there).
_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0)

# Modest pool — the catalog/health path is low-concurrency. The bigger
# wins come from ``keepalive_expiry`` so the connection survives between
# the 30-second health-poll cycles.
_LIMITS = httpx.Limits(
    max_keepalive_connections=10,
    max_connections=20,
    keepalive_expiry=30.0,
)

# Transient errors worth retrying. ``RemoteProtocolError`` covers the
# mid-call connection reset pattern from ollama/ollama#15910 (post 0.22.0
# upgrade); ``PoolTimeout`` covers our own pool exhaustion.
TRANSIENT_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)

# Circuit breaker policy. 5 failures opens the circuit; 30s cooldown
# before a single half-open trial. Threshold of 5 strikes a balance:
# small enough to fire promptly on a real outage, large enough to
# absorb single-blip noise without latching. Matches the SRE playbook
# default for external API breakers.
_FAILURE_THRESHOLD = 5
_RECOVERY_TIMEOUT_SECONDS = 30


# Module-level, mutable for tests; production code reads these directly.
# Created lazily on first use so the asyncio bindings (the client's
# ``AsyncClient`` instance, the breaker's lock) are bound to whatever
# event loop is current when called — matches the lazy-lock pattern
# documented in ``web/backend/CLAUDE.md``.
_client: httpx.AsyncClient | None = None
_breaker = CircuitBreaker(
    failure_threshold=_FAILURE_THRESHOLD,
    recovery_timeout=_RECOVERY_TIMEOUT_SECONDS,
    expected_exception=httpx.HTTPError,
    name="ollama-upstream",
)

# Tenacity wait strategy as a module attribute so tests can monkeypatch
# it to ``wait_none()`` for fast unit-test execution. Production uses
# exponential backoff with random jitter to spread retry attempts and
# avoid thundering-herd patterns on a recovered upstream.
_WAIT_STRATEGY = wait_exponential_jitter(initial=0.5, max=8.0)

# Sleep function exposed as a module attribute so tests can monkeypatch
# it to a no-op recorder. Production uses real ``asyncio.sleep``.
_async_sleep = asyncio.sleep


# --------------------------------------------------------------------------- #
# Client lifecycle                                                            #
# --------------------------------------------------------------------------- #


def get_client() -> httpx.AsyncClient:
    """Return the module-singleton ``AsyncClient``, constructing on first use.

    The lazy construction means the client is bound to the asyncio event
    loop that's current when this is first called. Under uvicorn that's
    the request loop; under pytest-asyncio it's the per-test loop. The
    ``_reset_for_tests()`` helper sets the singleton back to ``None`` so
    each test gets a fresh client bound to its own loop.

    HTTP/2 is enabled because Ollama Cloud advertises h2 and the
    multiplexing reduces the cost of the periodic 4-minute background
    refresh + the periodic 30-second health probe sharing one connection.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            http2=True,
            limits=_LIMITS,
            timeout=_TIMEOUT,
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    """Close the singleton client + null the reference.

    Called from the lifespan shutdown hook so we don't leak sockets when
    the app stops. Safe to call even if the client was never constructed
    (e.g. shutdown before first request).
    """
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------------- #
# Retry / breaker helpers                                                     #
# --------------------------------------------------------------------------- #


def _is_retryable_response(resp: httpx.Response) -> bool:
    """Return True iff the response status is 429 or 5xx.

    These are the documented Ollama Cloud transient-failure surface;
    everything else (200, 3xx redirects we don't follow, 4xx-non-429
    auth failures) is a definite answer, not a retry candidate.
    """
    return resp.status_code == 429 or 500 <= resp.status_code < 600


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header into a seconds-from-now float.

    Per RFC 7231 §7.1.3 the header may be either:

    * A non-negative integer delta-seconds (e.g. ``Retry-After: 30``).
    * An HTTP-date (e.g. ``Retry-After: Fri, 31 Dec 2099 23:59:59 GMT``).

    Returns ``None`` when the header is absent or unparseable — callers
    fall back to the tenacity wait strategy in that case.
    """
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001 — best-effort parse; absent on failure
        return None


class RetryableStatusError(httpx.HTTPError):
    """Sentinel exception for retryable 429/5xx responses after exhaustion.

    Subclasses ``httpx.HTTPError`` so the circuit breaker's
    ``expected_exception=httpx.HTTPError`` filter counts it as a
    failure (otherwise a sustained 503 burst would never open the
    breaker — the breaker only counts exceptions, not bad-status
    returns). Carries the originating ``Response`` for callers that
    want to inspect ``.response.status_code``, headers, body, or the
    Ollama-style ``(ref: ...)`` upstream identifier.

    Public so consumers (e.g. ``probe_model_liveness``) can catch
    it specifically and distinguish "upstream returned 503" (a definite
    "no") from a true transport timeout.
    """

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"retryable upstream status {response.status_code}")
        self.response = response


# Backward-compat alias for any internal imports during the refactor.
_RetryableUpstream = RetryableStatusError


# --------------------------------------------------------------------------- #
# Public request entry point                                                  #
# --------------------------------------------------------------------------- #


async def request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    json_body: dict | None = None,
    max_attempts: int = 3,
    max_total_seconds: float = 25.0,
) -> httpx.Response:
    """Issue one upstream request through retry + breaker.

    Args:
        method: HTTP method (``"GET"``, ``"POST"``, ...).
        url: Full upstream URL.
        headers: Optional request headers (e.g. ``Authorization``).
        json_body: Optional JSON request body.
        max_attempts: Cap on tenacity attempts including the first.
            Set to 1 to disable retries for a given call (used by
            paths with their own cache/fallback). Default 3.
        max_total_seconds: Wall-clock cap on the whole retry loop;
            tenacity stops once either ``max_attempts`` OR this is
            reached. Default 25s, leaves ample headroom inside the
            30-second SSE health-poll cadence.

    Raises:
        circuitbreaker.CircuitBreakerError: when the breaker is open;
            no transport call is attempted. Callers in
            ``ollama_models.py`` catch this and degrade to last-good
            cache.
        httpx.HTTPError (or a subclass): when retries exhaust without
            a non-retryable success. The breaker counts this as a
            failure and may open as a result.

    Returns:
        The ``httpx.Response`` on success (2xx, 3xx not followed, or
        4xx-non-429). 429/5xx are converted to ``_RetryableUpstream``
        and retried until budget exhausts.
    """
    client = get_client()

    @_breaker
    async def _do_request_with_retries() -> httpx.Response:
        attempt_idx = 0
        async for attempt in AsyncRetrying(
            stop=(
                stop_after_attempt(max_attempts)
                | stop_after_delay(max_total_seconds)
            ),
            wait=_WAIT_STRATEGY,
            retry=retry_if_exception_type(TRANSIENT_EXC + (RetryableStatusError,)),
            reraise=True,
        ):
            with attempt:
                attempt_idx += 1
                try:
                    resp = await client.request(
                        method, url, headers=headers, json=json_body
                    )
                except TRANSIENT_EXC as exc:
                    log.info(
                        "upstream_http.retry_attempt",
                        extra={
                            "url": url,
                            "attempt": attempt_idx,
                            "exception": repr(exc),
                        },
                    )
                    raise

                if _is_retryable_response(resp):
                    delay = _retry_after_seconds(resp)
                    if delay is not None:
                        log.info(
                            "upstream_http.retry_after_honored",
                            extra={
                                "url": url,
                                "status": resp.status_code,
                                "delay_seconds": delay,
                            },
                        )
                        await _async_sleep(min(delay, 30.0))
                    log.info(
                        "upstream_http.retry_attempt",
                        extra={
                            "url": url,
                            "attempt": attempt_idx,
                            "status": resp.status_code,
                        },
                    )
                    raise RetryableStatusError(resp)
                return resp
        # Tenacity with ``reraise=True`` always raises on exhaustion
        # (because we always raise inside the loop on retryable
        # conditions), so this is genuinely unreachable.
        raise AssertionError("unreachable: tenacity loop exited without return or raise")

    return await _do_request_with_retries()


# --------------------------------------------------------------------------- #
# Breaker state inspection                                                    #
# --------------------------------------------------------------------------- #


def circuit_state() -> str:
    """Return the current circuit-breaker state.

    Returns one of ``"closed"`` (normal), ``"open"`` (cooling down,
    requests short-circuit with ``CircuitBreakerError``), or
    ``"half_open"`` (one trial probe in flight after recovery_timeout
    elapsed). Used by ``GET /api/health`` to surface the state to the
    frontend so the UI can render the "recovering" pill instead of
    the red alert.
    """
    return _breaker.state


def _reset_for_tests() -> None:
    """Restore module state to a pristine condition.

    Called from a per-test autouse fixture so the singleton client +
    breaker state don't leak across tests. Mirrors the
    ``_reset_for_tests`` pattern in ``ollama_models.py`` and
    ``event_bus.py``.
    """
    global _client
    _client = None
    # CircuitBreaker doesn't expose a public ``reset()`` that touches
    # ``_state`` AND the timing fields together; we set the documented
    # internal attributes directly. This is the pattern used by the
    # package's own test suite (see circuitbreaker's tests/).
    _breaker._failure_count = 0
    _breaker._state = "closed"
    _breaker._opened = 0


__all__ = [
    "RetryableStatusError",
    "TRANSIENT_EXC",
    "circuit_state",
    "close_client",
    "get_client",
    "request",
]
