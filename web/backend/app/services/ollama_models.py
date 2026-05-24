"""Live discovery + liveness-probe of Ollama / Ollama Cloud models.

`GET {OLLAMA_BASE_URL}/models` returns the OpenAI-shaped list that works
for both local ``ollama serve`` and Ollama Cloud (``https://ollama.com/v1``).
Successful responses are cached for 5 minutes keyed by ``base_url``. On
failure the last-good cached value is returned if one exists, otherwise
an empty list — the catalog endpoint that fans out to this MUST stay
fast and MUST NOT 500 on upstream blips.

Why no exceptions escape: this service is called synchronously from the
catalog handler that builds the provider/model picker. A 5xx or a
hung Ollama would otherwise cascade into a broken settings page; the
contract is "best-effort, fast, never raise". The DEBUG log retains the
underlying error for ops triage.

v0.2.5+hf.4 changes (the deep-resilience pass)
==============================================
* Every HTTP call now routes through ``app.services.upstream_http``
  which carries the singleton client, tenacity retry layer, circuit
  breaker, and Retry-After header honouring. We never construct
  ``httpx.AsyncClient`` directly here anymore — the asymmetry with the
  run pipeline's resilience config (connect=10s, max_retries=5) is
  exactly what caused the user-visible alert to flap on a 2-second
  TCP RTT spike.
* ``_last_attempts`` is a rolling deque of the last 3 attempt outcomes
  per ``base_url``. ``last_probe_status()`` applies a 2-of-3 hysteresis
  rule before returning ``"down"`` — a single transient failure with
  two prior successes now stays ``"ok"`` and never surfaces to the user.
* ``list_ollama_models()`` implements stale-while-revalidate: when the
  cache is expired but populated, it returns the stale list immediately
  and schedules a background refresh task. The user-facing call never
  blocks on a cold fetch after the first one (which is itself pre-warmed
  by the ``upstream_warmup`` lifespan hook).
* On ``CircuitBreakerError`` from ``upstream_http`` (breaker is OPEN),
  the service falls back to the last-good cache + records the attempt
  as failed so ``last_probe_status()`` reports the situation honestly.

Two parallel pieces of state per ``base_url``:

* ``_cache`` — only updated on **success**. The catalog consumer reads
  from here and uses the last-good list when upstream is briefly down.
* ``_last_attempts`` — a ``deque[3]`` updated on **every** fetch attempt
  (success OR failure). The health endpoint reads this via
  ``last_probe_status()`` (hysteresis applied) and ``recent_attempts()``
  (raw rolling-3 view for UI) so the user sees an honest picture of the
  upstream's behavior.

The probe (POST /v1/chat/completions with the ``ping`` tool) keeps the
same liveness-probe contract as before — same return shape, same cache
TTLs (60s healthy / 30s unhealthy) — and now also routes through
``upstream_http``, getting retry + breaker for free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Literal, Optional, Tuple

import httpx
from circuitbreaker import CircuitBreakerError

from app.services import upstream_http

log = logging.getLogger(__name__)

_TTL_SECONDS = 300.0

# Probe cache TTLs. Healthy results stay for 60s — that covers the
# dual-model picker on NewRun plus the back-to-back retry click — and
# unhealthy results expire after 30s so a recovered upstream becomes
# visible within one health-probe cycle without us hammering it. Keyed
# by ``(base_url, model_id)`` so an OLLAMA_BASE_URL change invalidates
# the cache the same way it invalidates the list cache.
_PROBE_HEALTHY_TTL = 60.0
_PROBE_UNHEALTHY_TTL = 30.0

# Hysteresis: how many recent attempts to consider, and how many of
# those must be failures before we flip ``last_probe_status()`` to
# ``"down"``. 2-of-3 is the load-bearing user-visible contract — a
# single 2-second TCP spike no longer turns the alert red.
_HYSTERESIS_WINDOW = 3
_HYSTERESIS_FAILURE_THRESHOLD = 2

ProbeOutcome = Literal[
    "ok", "timeout", "http_5xx", "http_4xx", "degraded_empty_response"
]


class ProbeResult(dict):
    """Outcome of a single model liveness probe.

    A plain dict (rather than a TypedDict / dataclass) so the value can
    be embedded into a Pydantic ``RunValidationError`` detail body
    without an explicit conversion step. The shape is fixed by
    convention — see ``probe_model_liveness`` for the writer:

    * ``model``        — the probed model id (str)
    * ``status``       — one of ``ProbeOutcome`` literal values
    * ``upstream_ref`` — Optional[str]; populated for ``http_5xx`` when
                         Ollama's body matches ``(ref: <uuid-ish>)``
    * ``checked_at``   — monotonic timestamp (float) — used by the
                         caching layer to expire entries
    """


# (base_url, model_id) -> (cached_at_monotonic, ProbeResult)
_probe_cache: dict[tuple[str, str], tuple[float, "ProbeResult"]] = {}
_probe_lock: Optional[asyncio.Lock] = None

# Compiled once at module load; recognises Ollama's standard upstream
# error reference format ``(ref: <uuid-ish>)``. Used to extract the
# upstream-ref out of 5xx error bodies so the UI can surface it.
_UPSTREAM_REF_RE = re.compile(r"\(ref:\s*([0-9a-fA-F][0-9a-fA-F\-]{6,})\)")

#: base_url -> (fetched_at_monotonic, models) — ONLY updated on success.
_cache: dict[str, tuple[float, list[str]]] = {}

#: base_url -> deque[(attempted_at_monotonic, success, error_repr|None)].
#: Updated on EVERY fetch attempt so the health endpoint can tell
#: "1 transient with 2 prior successes" (no alert) from "2-of-3 failures"
#: (real outage, alert). Reset by ``_reset_for_tests``.
_last_attempts: dict[str, Deque[tuple[float, bool, Optional[str]]]] = {}

_lock: Optional[asyncio.Lock] = None

#: base_url -> currently-in-flight background refresh task.
#: ``list_ollama_models`` uses this to avoid scheduling concurrent
#: refreshes when stale-while-revalidate fires multiple times.
_in_flight_refresh: dict[str, asyncio.Task] = {}

ProbeStatus = Literal["ok", "down", "unknown"]


def _get_lock() -> asyncio.Lock:
    """Lazy-instantiate the lock so it binds to the current loop.

    See ``web/backend/CLAUDE.md`` — a module-level ``asyncio.Lock()``
    binds to whichever loop is current at import time. Under
    pytest-asyncio that loop is dead by the time the test awaits, raising
    ``RuntimeError: <Lock> is bound to a different event loop``.
    """
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _resolve_base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"


def _build_auth_headers() -> dict[str, str]:
    """Build the optional ``Authorization: Bearer ...`` header for Ollama Cloud."""
    api_key = os.environ.get("OLLAMA_API_KEY")
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


# --------------------------------------------------------------------------- #
# Attempt-log + hysteresis                                                    #
# --------------------------------------------------------------------------- #


def _interpret_attempts(
    log_deque: Deque[tuple[float, bool, Optional[str]]],
) -> Tuple[ProbeStatus, Optional[str]]:
    """Apply the 2-of-3 hysteresis rule to a rolling attempt log.

    Returns ``("ok", None)`` when:
    * the deque is non-empty AND
    * fewer than ``_HYSTERESIS_FAILURE_THRESHOLD`` of the entries are failures.

    Returns ``("down", <latest-error>)`` when failures meet/exceed the
    threshold. The latest error is surfaced (not the oldest) because it's
    the most actionable for triage.

    Returns ``("unknown", None)`` only when the deque is empty (caller
    handles the "no attempts yet" case separately).
    """
    if not log_deque:
        return ("unknown", None)
    failures = sum(1 for _, ok, _ in log_deque if not ok)
    if failures >= _HYSTERESIS_FAILURE_THRESHOLD:
        # The newest failure's error is the most actionable. We could
        # also surface a count ("3 of last 3 failed") but the existing
        # OllamaHealth schema only has a single ``error`` field.
        latest_err = next(
            (err for _, ok, err in reversed(log_deque) if not ok and err),
            "upstream unhealthy",
        )
        return ("down", latest_err)
    return ("ok", None)


def _record_attempt(
    base_url: str, *, success: bool, error: Optional[str]
) -> None:
    """Append to the rolling-3 attempt log; log a transition when the
    interpreted status changes (closed→open or vice versa).

    Keeping the transition log here (rather than in the call site) means
    every code path that records an attempt also benefits from the
    structured ``ollama_models.health_transition`` event.
    """
    log_deque = _last_attempts.setdefault(
        base_url, deque(maxlen=_HYSTERESIS_WINDOW)
    )
    prior_status, _ = _interpret_attempts(log_deque)
    log_deque.append((time.monotonic(), success, error))
    new_status, _ = _interpret_attempts(log_deque)
    if prior_status != new_status and "unknown" not in (prior_status,):
        log.info(
            "ollama_models.health_transition",
            extra={
                "from": prior_status,
                "to": new_status,
                "base_url": base_url,
            },
        )


# --------------------------------------------------------------------------- #
# Background refresh                                                          #
# --------------------------------------------------------------------------- #


def _schedule_background_refresh(base_url: str) -> None:
    """Schedule a refresh task if one isn't already in flight.

    Idempotent — if a refresh is already running for this base_url, we
    don't pile on another one. The task is stored in
    ``_in_flight_refresh`` so subsequent stale-while-revalidate calls
    can see it as in-flight.

    Errors inside the background task are swallowed by ``_fetch_now``;
    the task always completes (success or recorded-failure).
    """
    existing = _in_flight_refresh.get(base_url)
    if existing is not None and not existing.done():
        return

    async def _do_refresh() -> None:
        try:
            await _fetch_now(base_url)
        finally:
            _in_flight_refresh.pop(base_url, None)

    task = asyncio.create_task(
        _do_refresh(), name=f"ollama-refresh:{base_url}"
    )
    _in_flight_refresh[base_url] = task


# --------------------------------------------------------------------------- #
# Catalog list                                                                #
# --------------------------------------------------------------------------- #


async def _fetch_now(base_url: str) -> list[str]:
    """Issue one /models fetch through ``upstream_http`` + record outcome.

    Updates ``_cache`` on success and ``_last_attempts`` on every
    attempt. Never raises — exceptions are caught, recorded, and the
    last-good cache (or ``[]``) is returned.
    """
    headers = _build_auth_headers()
    url = base_url.rstrip("/") + "/models"

    try:
        resp = await upstream_http.request(
            "GET", url, headers=headers, max_attempts=3, max_total_seconds=25.0
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        models = [
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
    except CircuitBreakerError as exc:
        log.warning(
            "ollama_models.circuit_open_fetch_blocked",
            extra={"url": url, "error": repr(exc)},
        )
        _record_attempt(base_url, success=False, error=repr(exc))
        cached = _cache.get(base_url)
        return cached[1] if cached is not None else []
    except Exception as exc:  # noqa: BLE001 — never raise; catalog must stay responsive
        log.warning(
            "ollama_models.fetch_failed",
            extra={"url": url, "error": repr(exc)},
        )
        _record_attempt(base_url, success=False, error=repr(exc))
        cached = _cache.get(base_url)
        return cached[1] if cached is not None else []

    _record_attempt(base_url, success=True, error=None)
    _cache[base_url] = (time.monotonic(), models)
    return models


async def list_ollama_models() -> list[str]:
    """Return the upstream model id list, cached for ``_TTL_SECONDS``.

    Behavior:
    * Cache hit + fresh → return immediately.
    * Cache hit + stale → return stale list immediately; schedule a
      background refresh (stale-while-revalidate). The user-facing
      catalog endpoint stays snappy and the cache renews concurrently.
    * Cache miss → do a synchronous fetch.

    Never raises — failures fall through to the last-good cache or to
    an empty list when nothing is cached.
    """
    base_url = _resolve_base_url()
    now = time.monotonic()
    cached = _cache.get(base_url)

    if cached is not None:
        age = now - cached[0]
        if age < _TTL_SECONDS:
            return cached[1]
        # Stale-while-revalidate: return the stale list immediately and
        # schedule a background refresh. The age-log line gives ops a
        # signal that the user-facing path is on the stale-serve path.
        log.info(
            "ollama_models.stale_served",
            extra={"base_url": base_url, "cache_age_seconds": age},
        )
        _schedule_background_refresh(base_url)
        return cached[1]

    # Cold path: nothing cached yet. Do a synchronous fetch with the
    # full retry budget. The ``upstream_warmup`` lifespan hook pre-warms
    # this on startup so users rarely hit the cold path themselves.
    async with _get_lock():
        # Re-check inside the lock — a concurrent task may have populated it.
        cached = _cache.get(base_url)
        if cached is not None and (time.monotonic() - cached[0]) < _TTL_SECONDS:
            return cached[1]
        return await _fetch_now(base_url)


# --------------------------------------------------------------------------- #
# Status reads (read by the /api/health endpoint)                             #
# --------------------------------------------------------------------------- #


def last_probe_status() -> Tuple[ProbeStatus, Optional[str]]:
    """Return the hysteresis-filtered status of recent attempts.

    See ``_interpret_attempts`` for the rule. The contract:

    * ``("ok", None)``       — no recent failures, OR a single failure
                               with two prior successes (hysteresis).
    * ``("down", err_repr)`` — 2+ of the last 3 attempts failed.
    * ``("unknown", None)``  — no attempts recorded for this base_url
                               in this process yet (cold start, never
                               probed).

    Used by ``GET /api/health`` to populate ``ollama.status``. Crucially
    the outer health response keeps ``status: "ok"`` even when ollama
    is "down" — Coolify must not restart the container for an upstream
    LLM blip.
    """
    base_url = _resolve_base_url()
    log_deque = _last_attempts.get(base_url)
    if log_deque is None:
        return ("unknown", None)
    return _interpret_attempts(log_deque)


def recent_attempts() -> list[dict]:
    """Return the rolling-3 attempt log as a list of dicts.

    Used by ``GET /api/health`` to expose recent outcomes to the
    frontend so it can render a small "last 3 polls" indicator.

    Timestamps are converted from ``time.monotonic()`` (relative) to
    ISO8601 wallclock approximations via the current wallclock minus
    the relative age — accurate enough for "minutes ago" UX, not for
    forensic precision.
    """
    base_url = _resolve_base_url()
    log_deque = _last_attempts.get(base_url)
    if not log_deque:
        return []
    now_monotonic = time.monotonic()
    now_wallclock = datetime.now(timezone.utc)
    out: list[dict] = []
    for at_mono, ok, err in log_deque:
        delta_seconds = now_monotonic - at_mono
        wallclock_ts = now_wallclock.timestamp() - delta_seconds
        iso = datetime.fromtimestamp(wallclock_ts, tz=timezone.utc).isoformat()
        out.append({"at": iso, "ok": ok, "error": err})
    return out


# --------------------------------------------------------------------------- #
# Test reset                                                                  #
# --------------------------------------------------------------------------- #


def _reset_for_tests() -> None:
    """Test helper — null the locks and clear all cached state.

    Mirrors ``event_bus.reset_for_tests()``. Required because
    pytest-asyncio gives each test its own loop; a stale lock from a
    previous test crashes with "bound to a different event loop". Also
    clears ``_last_attempts`` so tests start from the "unknown" state.

    Also resets the shared ``upstream_http`` singletons (client +
    breaker) so breaker state from a previous test doesn't bleed into
    the next one.
    """
    global _lock, _probe_lock
    _lock = None
    _probe_lock = None
    _cache.clear()
    _last_attempts.clear()
    _probe_cache.clear()

    # Cancel any in-flight background refreshes so the next test starts
    # clean. Each test should drive a known transport behavior.
    for task in _in_flight_refresh.values():
        if not task.done():
            task.cancel()
    _in_flight_refresh.clear()

    upstream_http._reset_for_tests()


# --------------------------------------------------------------------------- #
# Model-liveness probe                                                        #
# --------------------------------------------------------------------------- #


def _get_probe_lock() -> asyncio.Lock:
    """Lazy probe lock — same loop-binding rationale as ``_get_lock``."""
    global _probe_lock
    if _probe_lock is None:
        _probe_lock = asyncio.Lock()
    return _probe_lock


def _probe_payload(model_id: str) -> dict:
    """Build the chat/completions body that exercises the upstream path.

    Reasoning models need a real ``max_completion_tokens`` budget — they
    fail open with empty content otherwise, which the probe correctly
    flags as ``degraded_empty_response`` but is wasteful when we can
    just give them the budget. Detection is twofold:

    1. The capability table flag ``requires_reasoning_split`` (set for
       MiniMax M2.x family) — that's the authoritative signal.
    2. A name-based ``"thinking"`` substring (covers kimi-k2-thinking
       and any future ``*-thinking`` SKU not yet in the capabilities
       table). Cheap, false-positive-tolerant.

    The ``tools=[...]`` block + default ``tool_choice="auto"`` is the
    payload shape that triggers the upstream tool-call 500 in
    ollama/ollama#14542. Removing it would defeat the probe.
    """
    try:
        from tradingagents.llm_clients.capabilities import get_capabilities

        caps = get_capabilities(model_id)
        is_reasoning = bool(getattr(caps, "requires_reasoning_split", False))
    except Exception:  # noqa: BLE001 — capability lookup is best-effort
        is_reasoning = False

    if "thinking" in model_id.lower():
        is_reasoning = True

    return {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_completion_tokens": 200 if is_reasoning else 1,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "ping",
                    "description": "Health probe — never invoked",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                },
            }
        ],
        # tool_choice intentionally omitted — defaults to "auto", which
        # is the path that triggered the upstream 500 in #14542.
    }


def _classify_completion(payload: dict) -> tuple[ProbeOutcome, Optional[str]]:
    """Classify a 200 chat-completion as ``ok`` or ``degraded_empty_response``.

    "Degraded" means HTTP 200 (so the upstream thinks it succeeded) but
    the assistant message is unusable: empty content, no tool calls,
    and a non-``stop`` finish_reason (typically ``length``, occasionally
    ``error``). That happens with reasoning models on a too-small token
    budget or when the upstream returns a sentinel completion.
    """
    choices = payload.get("choices") or []
    if not choices:
        return ("degraded_empty_response", None)
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    finish_reason = first.get("finish_reason")

    is_empty = (
        (not isinstance(content, str) or content == "")
        and not tool_calls
        and finish_reason != "stop"
    )
    if is_empty:
        return ("degraded_empty_response", None)
    return ("ok", None)


def _extract_upstream_ref(payload: dict, text: str | None) -> Optional[str]:
    """Pull an Ollama ``(ref: ...)`` upstream identifier from an error body.

    Tries the JSON ``error`` string first (the standard shape), then
    falls back to the raw text body. Returns ``None`` if no ref is
    present — the UI omits the field in that case.
    """
    candidates: list[str] = []
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, str):
        candidates.append(err)
    elif isinstance(err, dict):
        msg = err.get("message")
        if isinstance(msg, str):
            candidates.append(msg)
    if text:
        candidates.append(text)
    for candidate in candidates:
        match = _UPSTREAM_REF_RE.search(candidate)
        if match:
            return match.group(1)
    return None


async def probe_model_liveness(model_id: str) -> ProbeResult:
    """Send a tiny chat/completions request to verify ``model_id`` is alive.

    Returns a ``ProbeResult`` describing the outcome. **Never raises** —
    every exception is folded into a structured result so callers can
    aggregate multiple probes without try/except per-call.

    Cached per ``(base_url, model_id)`` with separate TTLs for healthy
    (60s) and unhealthy (30s) results. Routes through ``upstream_http``
    so the probe gets retry + breaker for free; the breaker is shared
    with the catalog list so a sustained outage opens once for both.
    """
    base_url = _resolve_base_url()
    cache_key = (base_url, model_id)
    now = time.monotonic()

    cached = _probe_cache.get(cache_key)
    if cached is not None:
        cached_at, prior = cached
        is_healthy = prior.get("status") == "ok"
        ttl = _PROBE_HEALTHY_TTL if is_healthy else _PROBE_UNHEALTHY_TTL
        if (now - cached_at) < ttl:
            return prior

    async with _get_probe_lock():
        # Re-check inside the lock — a concurrent probe may have just
        # populated the entry while we were waiting.
        cached = _probe_cache.get(cache_key)
        if cached is not None:
            cached_at, prior = cached
            is_healthy = prior.get("status") == "ok"
            ttl = _PROBE_HEALTHY_TTL if is_healthy else _PROBE_UNHEALTHY_TTL
            if (time.monotonic() - cached_at) < ttl:
                return prior

        headers = {"Content-Type": "application/json", **_build_auth_headers()}
        url = base_url.rstrip("/") + "/chat/completions"
        body = _probe_payload(model_id)

        outcome: ProbeOutcome
        upstream_ref: Optional[str] = None

        try:
            resp = await upstream_http.request(
                "POST",
                url,
                headers=headers,
                json_body=body,
                # Probe is already cached; one retry is enough — full 3-budget
                # would amplify upstream load during a flap.
                max_attempts=2,
                max_total_seconds=20.0,
            )
            status_code = resp.status_code
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {}

            # 4xx-non-429 returns as a Response (upstream_http only treats
            # 429 + 5xx as retryable). 5xx after exhaustion is raised as
            # RetryableStatusError — handled below.
            if status_code >= 500:
                outcome = "http_5xx"
                text = getattr(resp, "text", None)
                upstream_ref = _extract_upstream_ref(payload, text)
            elif status_code >= 400:
                outcome = "http_4xx"
                text = getattr(resp, "text", None)
                upstream_ref = _extract_upstream_ref(payload, text)
            else:
                outcome, upstream_ref = _classify_completion(payload)
        except upstream_http.RetryableStatusError as exc:
            # Tenacity exhausted retries on a 429 or 5xx; the last response
            # is wrapped on the exception. Classify it the same way we would
            # a direct response so the probe contract stays consistent.
            resp = exc.response
            try:
                payload = resp.json()
            except Exception:  # noqa: BLE001
                payload = {}
            text = getattr(resp, "text", None)
            if resp.status_code >= 500:
                outcome = "http_5xx"
                upstream_ref = _extract_upstream_ref(payload, text)
            else:
                # 429 lands here — treat as http_4xx for the probe outcome
                # since the user's UI flow doesn't distinguish them.
                outcome = "http_4xx"
                upstream_ref = _extract_upstream_ref(payload, text)
        except CircuitBreakerError as exc:
            # Breaker is OPEN — don't even try; report as timeout so the
            # caller can fail-fast without piling on the upstream.
            log.warning(
                "ollama_models.probe_circuit_open",
                extra={"url": url, "model": model_id, "error": repr(exc)},
            )
            outcome = "timeout"
        except (httpx.TimeoutException, httpx.ReadTimeout):
            outcome = "timeout"
        except Exception as exc:  # noqa: BLE001 — never raise
            # Treat unrecognised transport errors as timeouts for UX
            # purposes — they mean "we couldn't get a usable answer".
            log.warning(
                "ollama_models.probe_failed",
                extra={"url": url, "model": model_id, "error": repr(exc)},
            )
            outcome = "timeout"

        result: ProbeResult = ProbeResult(
            {
                "model": model_id,
                "status": outcome,
                "upstream_ref": upstream_ref,
                "checked_at": time.monotonic(),
            }
        )
        _probe_cache[cache_key] = (time.monotonic(), result)
        return result


def cached_probe_unhealthy_models() -> list[str]:
    """Return models whose last cached probe was unhealthy and still fresh.

    Used by the suggested-alternatives algorithm in the runs router so
    we don't recommend a model we've recently learned is broken. Only
    considers entries for the current ``base_url`` (matching the cache
    key shape) and applies the unhealthy TTL.
    """
    base_url = _resolve_base_url()
    now = time.monotonic()
    out: list[str] = []
    for (url, model_id), (cached_at, result) in _probe_cache.items():
        if url != base_url:
            continue
        if result.get("status") == "ok":
            continue
        if (now - cached_at) >= _PROBE_UNHEALTHY_TTL:
            continue
        out.append(model_id)
    return out


__all__ = [
    "ProbeOutcome",
    "ProbeResult",
    "ProbeStatus",
    "cached_probe_unhealthy_models",
    "last_probe_status",
    "list_ollama_models",
    "probe_model_liveness",
    "recent_attempts",
    "_reset_for_tests",
]
