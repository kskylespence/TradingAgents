"""Pre-flight liveness probe for /api/runs and /api/runs/{id}/retry.

A real run on 2026-05-22 hung for 56 minutes because ``POST /api/runs``
enqueued the engine on a model (``kimi-k2-thinking``) that was
unresponsive on Ollama Cloud — no defense checked model liveness before
launching. This layer adds a synchronous pre-flight probe (one try, the run's read timeout)
that pings each selected model and returns a structured 400 with healthy
alternatives when a model is dead.

The probe contract:

* Issues ``POST {base_url}/chat/completions`` with a ``ping`` user
  message, a no-op ``ping`` tool function, and ``tool_choice`` left at
  the default (``"auto"``) — that's the exact path that triggered the
  ``qwen3-coder:480b`` tool-call 500 in ollama/ollama#14542.
* For reasoning models (``requires_reasoning_split`` capability OR
  ``"thinking"`` in the model id), uses ``max_completion_tokens=200``
  to give the model budget to emit a non-empty response. Non-reasoning
  models get ``max_completion_tokens=1`` for the cheapest possible
  probe.
* One try per model with the run's own read timeout; a read
  timeout is not retried. Quick and deep probe at the same time.
* A 429 (Ollama Cloud's account usage limit) reports ``rate_limited``.
* Caches healthy results for 60s and unhealthy results for 30s (keyed
  by ``(base_url, model_id)``) so the catalog tab refreshing every
  ~30s and the dual-model select on NewRun (quick+deep) don't multiply
  upstream calls.

The validate-and-probe wiring runs on the create AND retry paths so a
user smashing the Retry button on a failed run can't queue the same
broken-model lifecycle a second time.

Tests follow ``test_runs_validate_model.py`` patterns — fake
``httpx.AsyncClient`` monkey-patched into the service module, ``TestClient``
with auth + CSRF bypassed via dependency_overrides and the global CSRF
predicate stub.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
from typing import Any

import httpx
import pytest
from app.middleware.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from fastapi.testclient import TestClient

from tests.helpers import TEST_ADMIN_ID

CSRF_TOKEN = "test-csrf-token-preflight"


# --------------------------------------------------------------------------- #
# Fake httpx probe client — installs into ollama_models.httpx.AsyncClient.    #
# Mirrors the pattern in test_ollama_models_service.py but mocks ``post``    #
# (not ``get``) because chat/completions is a POST endpoint.                  #
# --------------------------------------------------------------------------- #


def _install_probe_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    # `responses` maps model_id -> response spec. Default = healthy 200.
    # A spec is either a dict shaped like an OpenAI chat completion, or
    # ``{"status": int, "payload": dict}`` to control status, or
    # ``{"raise": Exception}`` to simulate a transport error.
    responses: dict[str, Any] | None = None,
    # `models_listing` is what the underlying /v1/models GET returns —
    # used by the suggested-alternatives algorithm. Defaults to a
    # sane curated set so the alternatives logic has something to chew on.
    models_listing: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Install a mock transport on the shared ``upstream_http`` client covering
    BOTH the GET /models catalog endpoint AND the POST /chat/completions
    probe endpoint.

    Returns a recorder dict the test asserts against::

        {"probe_calls": [{"model": "...", "payload": {...}}, ...],
         "list_calls": int}

    The probe is keyed on the ``model`` field of the JSON body, so a
    test can simulate "quick is healthy, deep is sick" by routing each
    model to a different spec.

    Implementation note (v0.2.5+hf.4): ``ollama_models`` now routes
    through ``upstream_http`` (a shared singleton client with retry +
    breaker). We mock at the transport layer so all the production
    wiring stays exercised; the transport just returns deterministic
    responses keyed by URL path.
    """
    record: dict[str, Any] = {"probe_calls": [], "list_calls": 0}
    responses = responses or {}
    models_listing = list(models_listing or [])

    def _default_healthy_completion(model_id: str) -> dict[str, Any]:
        return {
            "id": "chatcmpl-probe",
            "object": "chat.completion",
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "pong",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    def _build_probe_response(
        spec: Any, model_id: str, request: httpx.Request
    ) -> httpx.Response:
        if spec is None:
            return httpx.Response(
                200, json=_default_healthy_completion(model_id), request=request
            )
        if isinstance(spec, dict) and "raise" in spec:
            raise spec["raise"]
        if isinstance(spec, dict) and "status" in spec:
            return httpx.Response(
                spec["status"], json=spec.get("payload", {}), request=request
            )
        # Otherwise treat spec as a literal 200 payload (an OpenAI
        # completion shape — used for healthy / degraded-empty cases).
        return httpx.Response(200, json=spec, request=request)

    def _handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path.endswith("/models"):
            record["list_calls"] += 1
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": m} for m in models_listing],
                },
                request=request,
            )
        if "chat/completions" in url_path:
            import json as _json

            try:
                body = _json.loads(request.content or b"{}")
            except Exception:
                body = {}
            model_id = body.get("model")
            record["probe_calls"].append(
                {
                    "model": model_id,
                    "payload": body,
                    "timeout": request.extensions.get("timeout"),
                }
            )
            spec = responses.get(model_id)
            return _build_probe_response(spec, model_id, request)
        return httpx.Response(
            404, json={"error": "unknown path"}, request=request
        )

    from app.services import ollama_models, upstream_http

    # Reset state so prior tests' breaker/cache don't leak in.
    ollama_models._reset_for_tests()
    upstream_http._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        timeout=httpx.Timeout(5.0),
    )
    return record


# --------------------------------------------------------------------------- #
# TestClient fixture                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
def runs_client(monkeypatch):
    """TestClient with auth stubbed, CSRF bypassed, start_run spied."""
    from uuid import uuid4

    from app.main import app
    from app.routers.runs import get_current_user
    from app.services import run_service

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")

    calls: list[tuple] = []

    async def _spy_start_run(body, db, *, user_id=None):
        calls.append((body, db))
        return uuid4()

    monkeypatch.setattr(run_service, "start_run", _spy_start_run)

    from tests.helpers import make_auth_user

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")

    client = TestClient(app)
    client.cookies.set(CSRF_COOKIE_NAME, CSRF_TOKEN)

    try:
        yield client, calls
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _build_body(**overrides) -> dict:
    body = {
        "ticker": "NVDA",
        "analysis_date": "2026-05-21",
        "output_language": "English",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "ollama",
        "quick_think_llm": "glm-5",
        "deep_think_llm": "kimi-k2.6",
        "enable_checkpoint": False,
    }
    body.update(overrides)
    return body


def _post(client: TestClient, body: dict):
    return client.post(
        "/api/runs",
        json=body,
        headers={CSRF_HEADER_NAME: CSRF_TOKEN},
    )


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


def test_probe_timeout_returns_400(runs_client, monkeypatch) -> None:
    """ReadTimeout from the probe should surface as 400 with status='timeout'.

    The pre-existing failure mode was that a non-responsive model would
    cause the queued engine to hang for ~56 minutes. This rejects fast.
    """
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {"raise": httpx.ReadTimeout("read timeout after 15s")},
            "kimi-k2.6": {"raise": httpx.ReadTimeout("read timeout after 15s")},
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_model_unhealthy"
    assert detail["unhealthy_models"], "must list unhealthy models"
    statuses = {m["status"] for m in detail["unhealthy_models"]}
    assert statuses == {"timeout"}, statuses
    assert calls == [], "start_run must NOT be called when probe fails"


def test_probe_timeout_is_one_long_try_not_two_short_ones(
    runs_client, monkeypatch
) -> None:
    """A model gets ONE probe try, waiting as long as the run would.

    Two 15s tries refused glm-5.3-flash for a standard user on
    2026-11-14. A 45s try was still too short: that day both models took
    ~72s to answer "ping", while the run path waits 120s per call. The
    probe now uses the run's own read timeout, so it never refuses a
    model the run would accept; a hung one is still refused (see
    test_probe_timeout_returns_400) so it cannot pin the single run slot.
    """
    client, _calls = runs_client
    record = _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {"raise": httpx.ReadTimeout("read timeout")},
            "kimi-k2.6": None,
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    glm_calls = [c for c in record["probe_calls"] if c["model"] == "glm-5"]
    assert len(glm_calls) == 1, f"timeout must not be retried, got {len(glm_calls)}"
    from tradingagents.llm_clients.capabilities import resolve_read_timeout_seconds

    assert glm_calls[0]["timeout"]["read"] == 120.0, glm_calls[0]["timeout"]
    for call in record["probe_calls"]:
        expected = resolve_read_timeout_seconds(call["model"])
        assert call["timeout"]["read"] == expected, call


def test_probe_waits_as_long_as_the_run_for_that_model(
    runs_client, monkeypatch
) -> None:
    """The probe reads the same per-model timeout the run uses.

    The env override wins for both, so an operator who raises it for a
    slow day raises the check too.
    """
    client, _calls = runs_client
    monkeypatch.setenv("TRADINGAGENTS_LLM_READ_TIMEOUT", "77")
    record = _install_probe_fake(
        monkeypatch,
        responses={"glm-5": None, "kimi-k2.6": None},
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code != 400, resp.text
    assert record["probe_calls"], "probe never ran"
    for call in record["probe_calls"]:
        assert call["timeout"]["read"] == 77.0, call["timeout"]


def test_probe_429_usage_limit_reports_rate_limited(runs_client, monkeypatch) -> None:
    """Ollama's 429 'session usage limit' must read as a usage limit.

    On 2026-11-14 every probe returned 429 because the account hit its
    usage limit; the UI called it 'returned 4xx', which points no one
    at the fix (add credits or wait for the reset).
    """
    from app.services import upstream_http

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(upstream_http, "_async_sleep", _no_sleep)
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {
                "status": 429,
                "payload": {
                    "error": {
                        "message": "you (someone) have reached your session usage limit",
                        "type": "api_error",
                    }
                },
            },
            "kimi-k2.6": None,
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    unhealthy = {m["model"]: m for m in detail["unhealthy_models"]}
    assert unhealthy["glm-5"]["status"] == "rate_limited"
    assert "usage limit" in detail["message"].lower()
    assert calls == []


def test_probe_429_is_final_and_spares_the_circuit_breaker(
    runs_client, monkeypatch
) -> None:
    """A usage-limit 429 is not retried and does not trip the breaker.

    The breaker is shared with the catalog and health checks. Counting
    each 429 as a failure meant three submits opened it, after which
    probes reported ``timeout`` and the UI offered other models again,
    which is wrong when the whole account is limited.
    """
    from app.services import upstream_http

    client, _calls = runs_client
    record = _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {"status": 429, "payload": {"error": "usage limit"}},
            "kimi-k2.6": None,
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    glm_calls = [c for c in record["probe_calls"] if c["model"] == "glm-5"]
    assert len(glm_calls) == 1, f"429 must not be retried, got {len(glm_calls)}"
    assert upstream_http._breaker.failure_count == 0


def test_usage_limit_message_names_only_the_limited_models(
    runs_client, monkeypatch
) -> None:
    """Mixed failures: the usage-limit sentence blames only 429 models."""
    client, _calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {"status": 429, "payload": {"error": "usage limit"}},
            "kimi-k2.6": {"status": 500, "payload": {"error": "boom"}},
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    detail = resp.json()["detail"]
    assert "glm-5" in detail["message"]
    assert "kimi-k2.6" not in detail["message"]


def test_probe_has_a_hard_wall_clock_cap(runs_client, monkeypatch) -> None:
    """The whole probe, retries included, stops at the hard cap.

    Tenacity only checks its time budget between tries, so a slow 5xx
    followed by a retry could run ~90s while the UI says "up to a
    minute". Scaled down here: read timeout 0.25s + 0.05s slack = a 0.3s
    cap, each try answers 503 after 0.25s, so without a hard cap the
    retry runs past 0.5s.
    """
    import asyncio
    import time

    from app.services import ollama_models, upstream_http

    client, _calls = runs_client

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "glm-5"}]}, request=request
            )
        await asyncio.sleep(0.25)
        return httpx.Response(503, json={"error": "busy"}, request=request)

    ollama_models._reset_for_tests()
    upstream_http._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), timeout=httpx.Timeout(5.0)
    )
    monkeypatch.setenv("TRADINGAGENTS_LLM_READ_TIMEOUT", "0.25")
    monkeypatch.setattr(ollama_models, "_PROBE_HARD_CAP_SLACK_SECONDS", 0.05)

    started = time.monotonic()
    resp = _post(client, _build_body(quick_think_llm="glm-5", deep_think_llm="glm-5"))
    elapsed = time.monotonic() - started

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["unhealthy_models"][0]["status"] == "timeout"
    assert elapsed < 0.5, f"probe ran {elapsed:.2f}s past its 0.3s cap"


def test_probes_for_quick_and_deep_run_at_the_same_time(
    runs_client, monkeypatch
) -> None:
    """Different models are probed concurrently, not one after another.

    A single global probe lock made the two probes queue, so a slow
    model doubled the time the submit button spun.
    """
    import asyncio
    import json as _json

    from app.services import ollama_models, upstream_http

    client, calls = runs_client
    state = {"in_flight": 0, "max_in_flight": 0}

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "glm-5"}, {"id": "kimi-k2.6"}]},
                request=request,
            )
        model_id = _json.loads(request.content)["model"]
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        await asyncio.sleep(0.2)
        state["in_flight"] -= 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "model": model_id,
            },
            request=request,
        )

    ollama_models._reset_for_tests()
    upstream_http._client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), timeout=httpx.Timeout(5.0)
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 200, resp.text
    assert state["max_in_flight"] == 2, state
    assert len(calls) == 1


def test_probe_5xx_extracts_upstream_ref(runs_client, monkeypatch) -> None:
    """When Ollama returns 500 with `(ref: ...)`, the ref appears in detail."""
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {
                "status": 500,
                "payload": {
                    "error": "Internal Server Error (ref: fd44ca4b-1234-5678-abcd-deadbeef)"
                },
            },
            "kimi-k2.6": None,  # healthy
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "upstream_model_unhealthy"
    unhealthy = {m["model"]: m for m in detail["unhealthy_models"]}
    assert "glm-5" in unhealthy
    assert unhealthy["glm-5"]["status"] == "http_5xx"
    assert "fd44ca4b" in (unhealthy["glm-5"].get("upstream_ref") or "")
    assert calls == []


def test_probe_200_passes_through(runs_client, monkeypatch) -> None:
    """A healthy 200 with a non-empty completion lets the run start."""
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={"glm-5": None, "kimi-k2.6": None},  # both healthy defaults
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert "run_id" in body
    assert len(calls) == 1, "start_run should run when both models are healthy"


def test_probe_degraded_empty_response(runs_client, monkeypatch) -> None:
    """HTTP 200 with empty content + no tool_calls + finish_reason!='stop' is degraded."""
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": {
                "id": "chatcmpl-degraded",
                "object": "chat.completion",
                "model": "glm-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": None,
                        },
                        "finish_reason": "length",
                    }
                ],
            },
            "kimi-k2.6": None,
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    unhealthy = {m["model"]: m for m in detail["unhealthy_models"]}
    assert unhealthy["glm-5"]["status"] == "degraded_empty_response"
    assert calls == []


def test_mixed_quick_healthy_deep_unhealthy(runs_client, monkeypatch) -> None:
    """quick_think healthy + deep_think 5xx → 400 lists ONLY the deep model."""
    client, calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "glm-5": None,  # healthy
            "kimi-k2.6": {
                "status": 500,
                "payload": {"error": "Internal Server Error"},
            },
        },
        models_listing=["glm-5", "kimi-k2.6"],
    )

    resp = _post(client, _build_body())

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    unhealthy = [m["model"] for m in detail["unhealthy_models"]]
    assert unhealthy == ["kimi-k2.6"], (
        f"expected only the unhealthy deep_think model, got {unhealthy}"
    )
    assert calls == []


def test_retry_endpoint_inherits_probe(monkeypatch, tmp_path) -> None:
    """POST /api/runs/{id}/retry on a failed run must run the same probe.

    A user retrying a failed run via the UI shouldn't be able to bypass
    the liveness check — that would re-queue the same hang the run had
    been failed for in the first place.
    """
    import asyncio
    import uuid
    from datetime import date, datetime

    from app import (
        db as db_mod,
        models,  # noqa: F401 — register tables on Base.metadata
    )
    from app.auth import get_current_user
    from app.db import Base, get_session
    from app.main import app
    from app.models import Run
    from app.services import event_bus as eb_mod, run_service
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("FAKE_LLM", "1")

    # Per-test SQLite file engine so router + start_run + event_bus all
    # see the same data (same pattern as test_runs_retry.py).
    db_path = tmp_path / "preflight-retry.sqlite"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True
    )

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_setup())

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "get_session_factory", lambda: factory)
    monkeypatch.setattr(eb_mod, "get_session_factory", lambda: factory)
    eb_mod.reset_for_tests()
    import asyncio as _aio

    eb_mod._lock = _aio.Lock()

    # Seed a failed run that selected an Ollama model that will be
    # unhealthy on probe.
    parent_id = str(uuid.uuid4())

    async def _seed() -> None:
        async with factory() as session:
            session.add(
                Run(
                    id=parent_id,
                    user_id=TEST_ADMIN_ID,
                ticker="RETRYME",
                    asset_type="stock",
                    analysis_date=date(2026, 5, 19),
                    analysts=["market"],
                    research_depth=1,
                    llm_provider="ollama",
                    quick_think_llm="kimi-k2-thinking",
                    deep_think_llm="kimi-k2-thinking",
                    output_language="English",
                    checkpoint_enabled=False,
                    status="failed",
                    started_at=datetime(2026, 5, 19, 12, tzinfo=UTC),
                    finished_at=datetime(
                        2026, 5, 19, 13, tzinfo=UTC
                    ),
                    error_message="upstream stuck",
                )
            )
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_seed())

    # Spy on start_run so we can prove it WASN'T called when probe fails.
    calls: list[tuple] = []

    async def _spy_start_run(body, db, *, user_id=None):
        calls.append((body, db))
        return uuid.uuid4()

    monkeypatch.setattr(run_service, "start_run", _spy_start_run)

    # Probe returns ReadTimeout for kimi-k2-thinking — the model that
    # caused the original 56-minute hang.
    _install_probe_fake(
        monkeypatch,
        responses={
            "kimi-k2-thinking": {
                "raise": httpx.ReadTimeout("read timeout after 15s"),
            },
        },
        models_listing=["kimi-k2-thinking", "glm-5"],
    )

    async def _override_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    from tests.helpers import make_auth_user

    app.dependency_overrides[get_current_user] = lambda: make_auth_user(username="tester")

    # Bypass CSRF (this endpoint is POST so the middleware would otherwise
    # demand the token — same dance test_runs_retry.py does).
    import app.middleware.csrf as csrf_mod

    orig_pred = csrf_mod._csrf_required
    csrf_mod._csrf_required = lambda method, path: False  # type: ignore[assignment]

    try:
        with TestClient(app) as client:
            resp = client.post(f"/api/runs/{parent_id}/retry")

        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "upstream_model_unhealthy"
        assert any(
            m["model"] == "kimi-k2-thinking" for m in detail["unhealthy_models"]
        )
        assert calls == [], "start_run must NOT be called on a failed probe"
    finally:
        csrf_mod._csrf_required = orig_pred
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_user, None)

        async def _dispose() -> None:
            await engine.dispose()

        asyncio.get_event_loop().run_until_complete(_dispose())


def test_suggested_alternatives_only_curated_and_healthy(
    runs_client, monkeypatch
) -> None:
    """Alternatives = curated AND in /v1/models AND not cached-unhealthy.

    Fixture exposes (per the brief):
      * ``glm-5.2`` (curated, healthy)
      * ``kimi-k2.6`` (curated, healthy)
      * ``random-model`` (NOT curated)
      * ``nemotron-3-super`` (curated, but probe-unhealthy in cache)

    The selected models in this run are both unhealthy so the response
    body MUST list alternatives. Expected: [``glm-5.2``, ``kimi-k2.6``]
    sorted alphabetically, with glm-5.2 first (curated headline).

    Was ``glm-5`` until the 2026-08-05 snapshot refresh, when Ollama
    retired it in favour of 5.1/5.2 and it stopped being curated.
    """
    client, _calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "broken-quick": {"raise": httpx.ReadTimeout("rip")},
            "broken-deep": {"raise": httpx.ReadTimeout("rip")},
            # Pre-warm the cache: prime nemotron-3-super as unhealthy.
            "nemotron-3-super": {
                "status": 500,
                "payload": {"error": "Internal Server Error"},
            },
            # Healthy curated members:
            "glm-5.2": None,
            "kimi-k2.6": None,
            # Non-curated noise that must NOT appear in alternatives:
            "random-model": None,
        },
        models_listing=[
            "glm-5.2",
            "kimi-k2.6",
            "nemotron-3-super",
            "random-model",
            "broken-quick",
            "broken-deep",
        ],
    )

    # Pre-warm the unhealthy cache for nemotron-3-super so the
    # alternatives algorithm excludes it. This is the model-probe cache
    # used by the validator, not the catalog cache.
    import asyncio

    from app.services.ollama_models import probe_model_liveness

    asyncio.get_event_loop().run_until_complete(
        probe_model_liveness("nemotron-3-super")
    )

    resp = _post(
        client,
        _build_body(
            quick_think_llm="broken-quick", deep_think_llm="broken-deep"
        ),
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    alternatives = detail["suggested_alternatives"]
    # Must NOT include the broken models, the non-curated random model,
    # or the unhealthy nemotron-3-super.
    assert "broken-quick" not in alternatives
    assert "broken-deep" not in alternatives
    assert "random-model" not in alternatives
    assert "nemotron-3-super" not in alternatives
    # MUST include the two healthy curated models.
    assert "glm-5.2" in alternatives
    assert "kimi-k2.6" in alternatives
    # At most 3.
    assert len(alternatives) <= 3
    # Sorted alphabetically with glm-5.2 first (curated headline).
    assert alternatives[0] == "glm-5.2"


def test_suggested_alternatives_pins_tagged_headline_model(
    runs_client, monkeypatch
) -> None:
    """The headline pin must survive a *tagged* GLM id.

    ``_suggested_alternatives`` builds its candidate list from the live
    ``/v1/models`` listing, which returns tagged ids
    (``deepseek-v4-flash:0731``). The pin compares against bare base names,
    so an exact-string check silently stops firing the moment GLM ships with
    a tag — and because the result is capped at 3, the headline model doesn't
    just lose its position, it can fall out of the response entirely once
    three other curated models sort ahead of it alphabetically.

    The three curated models below that sort alphabetically ahead of
    ``glm-5.2:cloud`` (``deepseek-v4-flash:0731``, ``deepseek-v4-pro``,
    ``gemma4:31b``) exist to fill the cap exactly, so the pre-fix code drops
    the headline model from the response entirely rather than merely demoting
    it — the failure this guards against, reproduced literally.
    """
    client, _calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "broken-quick": {"raise": httpx.ReadTimeout("rip")},
            "broken-deep": {"raise": httpx.ReadTimeout("rip")},
            "glm-5.2:cloud": None,
            "deepseek-v4-flash:0731": None,
            "deepseek-v4-pro": None,
            "gemma4:31b": None,
            "kimi-k2.6": None,
        },
        models_listing=[
            "glm-5.2:cloud",
            "deepseek-v4-flash:0731",
            "deepseek-v4-pro",
            "gemma4:31b",
            "kimi-k2.6",
            "broken-quick",
            "broken-deep",
        ],
    )

    resp = _post(
        client,
        _build_body(quick_think_llm="broken-quick", deep_think_llm="broken-deep"),
    )

    assert resp.status_code == 400, resp.text
    alternatives = resp.json()["detail"]["suggested_alternatives"]
    # Without the base-name pin this list is the first three alphabetically and
    # the headline model is absent altogether.
    assert "glm-5.2:cloud" in alternatives, (
        "tagged headline model was dropped from the capped list entirely"
    )
    assert alternatives[0] == "glm-5.2:cloud", (
        "tagged headline model must still be pinned first"
    )


def test_suggested_alternatives_pins_newest_glm_first(
    runs_client, monkeypatch
) -> None:
    """The newest GLM release leads the suggestions: 5.3, then 5.3-flash, then 5.2.

    Alphabetical order alone would put ``glm-5.2`` ahead of ``glm-5.3`` and
    let ``deepseek-v4-pro`` take a slot, so with the cap at 3 the newest
    flagship would sit behind the model it replaced.
    """
    client, _calls = runs_client
    _install_probe_fake(
        monkeypatch,
        responses={
            "broken-quick": {"raise": httpx.ReadTimeout("rip")},
            "broken-deep": {"raise": httpx.ReadTimeout("rip")},
            "deepseek-v4-pro": None,
            "glm-5.2": None,
            "glm-5.3": None,
            "glm-5.3-flash": None,
        },
        models_listing=[
            "deepseek-v4-pro",
            "glm-5.2",
            "glm-5.3",
            "glm-5.3-flash",
            "broken-quick",
            "broken-deep",
        ],
    )

    resp = _post(
        client,
        _build_body(quick_think_llm="broken-quick", deep_think_llm="broken-deep"),
    )

    assert resp.status_code == 400, resp.text
    alternatives = resp.json()["detail"]["suggested_alternatives"]
    assert alternatives == ["glm-5.3", "glm-5.3-flash", "glm-5.2"]


def test_probe_cache_dedup(runs_client, monkeypatch) -> None:
    """Two POSTs in the same TTL window should issue exactly one upstream probe."""
    client, _calls = runs_client
    rec = _install_probe_fake(
        monkeypatch,
        responses={"glm-5": None, "kimi-k2.6": None},
        models_listing=["glm-5", "kimi-k2.6"],
    )

    r1 = _post(client, _build_body())
    r2 = _post(client, _build_body())
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    # Two models, two POSTs, but cache means the second POST should
    # find both entries hot → expected total = 2 probes (one per model
    # across both requests combined).
    probed_models = [c["model"] for c in rec["probe_calls"]]
    assert sorted(set(probed_models)) == ["glm-5", "kimi-k2.6"]
    assert len(probed_models) == 2, (
        f"cache should dedup repeat probes; got {len(probed_models)}: {probed_models}"
    )


def test_non_ollama_providers_skip_probe(runs_client, monkeypatch) -> None:
    """provider=openai must NEVER call the Ollama probe.

    Other providers will get their own liveness checks in a follow-up
    pass; the Ollama-only restriction here keeps this layer focused.
    """
    client, calls = runs_client
    rec = _install_probe_fake(
        monkeypatch,
        responses={},  # nothing should be called
        models_listing=[],
    )

    resp = _post(
        client,
        _build_body(
            llm_provider="openai",
            quick_think_llm="gpt-5.4-mini",
            deep_think_llm="gpt-5.4",
        ),
    )

    assert resp.status_code == 200, resp.text
    assert rec["probe_calls"] == [], (
        f"non-Ollama provider must skip Ollama probe; got {rec['probe_calls']}"
    )
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Probe payload — the path that exercises ollama/ollama#14542                  #
# (tool_call 500 on qwen3-coder:480b when tool_choice="auto").                 #
# --------------------------------------------------------------------------- #


def test_probe_payload_includes_tools_and_reasoning_budget(
    monkeypatch,
) -> None:
    """Direct probe call: payload must include the ``ping`` tool and a
    reasoning-aware ``max_completion_tokens`` budget.

    This is the contract that makes the probe actually catch
    ollama/ollama#14542 — without ``tools=[...]`` the default
    ``tool_choice="auto"`` never fires, and without a ``max_completion_tokens``
    budget reasoning models like ``kimi-k2-thinking`` emit empty content.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    rec = _install_probe_fake(
        monkeypatch,
        responses={"glm-5": None, "kimi-k2-thinking": None},
        models_listing=[],
    )

    import asyncio

    from app.services.ollama_models import probe_model_liveness

    asyncio.get_event_loop().run_until_complete(probe_model_liveness("glm-5"))
    asyncio.get_event_loop().run_until_complete(
        probe_model_liveness("kimi-k2-thinking")
    )

    by_model = {c["model"]: c["payload"] for c in rec["probe_calls"]}
    glm = by_model["glm-5"]
    kimi = by_model["kimi-k2-thinking"]

    # Both must include the ``ping`` tool — that's the path that fires
    # the default tool_choice="auto" branch in Ollama (issue #14542).
    for payload in (glm, kimi):
        assert payload.get("stream") is False
        assert payload.get("messages") == [
            {"role": "user", "content": "ping"}
        ]
        tools = payload.get("tools") or []
        assert len(tools) == 1, "must include exactly one no-op tool"
        assert tools[0]["function"]["name"] == "ping"

    # Reasoning model gets a real budget; non-reasoning gets the cheap probe.
    assert glm.get("max_completion_tokens") == 1
    assert kimi.get("max_completion_tokens") == 200
