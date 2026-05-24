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
import re
import time
from typing import Literal, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

_TTL_SECONDS = 300.0
_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=5.0)

# Probe budget is intentionally tighter than the list-models budget so
# the pre-flight check stays inside ~15s even when an unresponsive model
# eats the full read window. Two models × 15s = 30s worst-case; in
# practice they run concurrently so the wall time is one read budget.
_PROBE_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# Probe cache TTLs. Healthy results stay for 60s — that covers the
# dual-model picker on NewRun plus the back-to-back retry click — and
# unhealthy results expire after 30s so a recovered upstream becomes
# visible within one health-probe cycle without us hammering it. Keyed
# by ``(base_url, model_id)`` so an OLLAMA_BASE_URL change invalidates
# the cache the same way it invalidates the list cache.
_PROBE_HEALTHY_TTL = 60.0
_PROBE_UNHEALTHY_TTL = 30.0

ProbeOutcome = Literal[
    "ok", "timeout", "http_5xx", "http_4xx", "degraded_empty_response"
]

# (base_url, model_id) -> (cached_at_monotonic, ProbeResult)
_probe_cache: dict[tuple[str, str], tuple[float, "ProbeResult"]] = {}
_probe_lock: Optional[asyncio.Lock] = None

# Compiled once at module load; recognises Ollama's standard upstream
# error reference format ``(ref: <uuid-ish>)``. Used to extract the
# upstream-ref out of 5xx error bodies so the UI can surface it.
_UPSTREAM_REF_RE = re.compile(r"\(ref:\s*([0-9a-fA-F][0-9a-fA-F\-]{6,})\)")

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
    global _lock, _probe_lock
    _lock = None
    _probe_lock = None
    _cache.clear()
    _last_attempt.clear()
    _probe_cache.clear()


# --------------------------------------------------------------------------- #
# Model-liveness probe                                                        #
# --------------------------------------------------------------------------- #


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
    (60s) and unhealthy (30s) results.
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

        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = os.environ.get("OLLAMA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = base_url.rstrip("/") + "/chat/completions"
        body = _probe_payload(model_id)

        outcome: ProbeOutcome
        upstream_ref: Optional[str] = None

        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=headers)
                status_code = resp.status_code
                try:
                    payload = resp.json()
                except Exception:  # noqa: BLE001
                    payload = {}

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
    "list_ollama_models",
    "last_probe_status",
    "ProbeStatus",
    "ProbeOutcome",
    "ProbeResult",
    "probe_model_liveness",
    "cached_probe_unhealthy_models",
    "_reset_for_tests",
]
