# Backend development

> Audience: contributors adding code under `web/backend/`. Read
> [`architecture.md`](architecture.md) first.

This guide covers the four most common backend tasks (add a router, add
middleware, add a lifespan hook, add a service) plus the security and
async-loop invariants you must respect.

For local install + running the test suite, see
[`../backend/dev-install.md`](../backend/dev-install.md). For test
patterns, see [`testing.md`](testing.md).

## The auto-discovery registry pattern

Three packages — `app/routers/`, `app/middleware/`,
`app/lifespan_hooks/` — each have a `__init__.py` that imports every
non-underscore submodule on package load via `pkgutil.iter_modules`.
Each submodule calls a registration function at module scope. The
result: **dropping a file IS the wiring**. `app/main.py` doesn't need
to know about your new file.

This is the pattern that lets multiple agents (human or otherwise)
add new files in parallel without ever conflicting on `main.py`.

## Adding an HTTP router

```python
# web/backend/app/routers/widgets.py
from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.schemas import AuthUser

from . import register

router = APIRouter(prefix="/widgets", tags=["widgets"])


@router.get("")
async def list_widgets(_user: AuthUser = Depends(get_current_user)):
    return [{"id": 1, "name": "example"}]


register(router)
```

That's it. Restart uvicorn and `GET /api/widgets` works.

### Routing pitfall: first-match wins

FastAPI matches routes in **first-registered-wins** order. Anything
registered inline on `app` in `main.py` (e.g. `@app.get("/api/health")`)
will shadow auto-discovered routers mounted at the same path. We
learned this the hard way when an `/api/health` placeholder in
`main.py` shadowed the real health router and made Coolify report
"ok" even when the DB was down.

The `/api/_bootstrap_health` placeholder is the one legitimate inline
route — it's the router-import-failure fallback. Don't add others
without a comparable reason.

### Trailing-slash gotcha

`@router.get("/")` with `prefix="/widgets"` mounts at `/api/widgets/`,
and FastAPI's `redirect_slashes=True` will redirect `/api/widgets` →
`/api/widgets/` (307). If you want exact `/api/widgets`, use
`@router.get("")` instead. The health router uses this trick.

## Adding HTTP middleware

```python
# web/backend/app/middleware/timing.py
import time
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from . import register


class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Process-Time"] = f"{time.monotonic() - start:.3f}"
        return response


def install(app: FastAPI) -> None:
    app.add_middleware(TimingMiddleware)


register(install)
```

### Ordering note

FastAPI applies middleware in **REVERSE registration order** — the
last `add_middleware` call wraps the request first. If your middleware
must run before/after a specific other one, document the ordering in
the module docstring. The simplest enforcement is filename order:
`pkgutil.iter_modules` returns submodules alphabetically, so
`_a_first.py`, `b_second.py`, `c_third.py` will register in that
order (and therefore wrap in reverse).

The existing middleware (`security_headers.py`, `csrf.py`) doesn't
care about order because one is response-side additive and the other
is request-side short-circuit.

## Adding a lifespan hook

```python
# web/backend/app/lifespan_hooks/cache_warmer.py
import asyncio
import logging
from fastapi import FastAPI

from . import on_startup, on_shutdown

log = logging.getLogger(__name__)
_task: asyncio.Task | None = None


@on_startup
async def start(app: FastAPI) -> None:
    global _task
    _task = asyncio.create_task(_loop())
    log.info("cache_warmer.started")


@on_shutdown
async def stop(app: FastAPI) -> None:
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    log.info("cache_warmer.stopped")


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(300)
            # warm the cache
        except asyncio.CancelledError:
            break
```

- Startup hooks run in registration order.
- Shutdown hooks run in **reverse** registration order so teardown
  unwinds setup cleanly.
- Startup exceptions abort startup (fail-fast). Shutdown exceptions
  are logged + swallowed so the rest of teardown completes.

## Adding a service

A service is just a Python module under `app/services/`. There is no
registration step — other code imports it directly. The convention is
to keep services pure (no global mutable state aside from caches and
locks) and async-friendly.

### The lazy-lock convention

Any module-level `asyncio.Lock()` MUST be lazy-initialized:

```python
# Good — binds to the running loop on first use
_lock: asyncio.Lock | None = None

def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def something():
    async with _get_lock():
        ...


def reset_for_tests() -> None:
    global _lock
    _lock = None
```

```python
# Bad — binds to whichever loop is current at import time
_lock = asyncio.Lock()  # ← will crash under uvicorn --reload or pytest-asyncio
```

A Lock created at import time grabs a reference to whatever event
loop happens to be running then. Under uvicorn `--reload` and under
pytest-asyncio (which gives each test its own loop), that loop is
dead by the time you await the lock, and you get
`RuntimeError: <Lock> is bound to a different event loop`.

References: `app/services/event_bus.py:_get_lock()`,
`app/services/rate_limit.py:LoginRateLimiter._get_lock()`,
`app/services/ollama_models.py:_get_lock()`.

### Outbound HTTP — use `services/upstream_http` (don't re-roll `httpx.AsyncClient`)

Every outbound HTTP call to a flaky upstream (today: Ollama Cloud)
goes through `app.services.upstream_http`. The module owns a singleton
`httpx.AsyncClient` with HTTP/2, `Limits()`, and a sane `Timeout`,
wraps it with tenacity retries + a `circuitbreaker.CircuitBreaker`,
and honours `Retry-After` headers. **Don't construct your own
`httpx.AsyncClient`** — you'll bypass the retry/breaker layers and the
asymmetry that the v0.2.5+hf.4 pass was specifically designed to
eliminate.

The public surface is small:

```python
from app.services import upstream_http

resp = await upstream_http.request(
    "GET", url,
    headers=headers,
    json_body=body,
    max_attempts=3,          # cap retries (1 = no retry)
    max_total_seconds=25.0,  # wall-clock cap across all retries
)
state = upstream_http.circuit_state()  # "closed" | "open" | "half_open"
```

`request()` may raise:

- `circuitbreaker.CircuitBreakerError` — breaker is open; degrade to
  cached / fallback data, don't propagate.
- `upstream_http.RetryableStatusError` — retries exhausted on 429/5xx;
  `.response` carries the last 5xx/429 `httpx.Response` so callers can
  classify (e.g. `probe_model_liveness` extracts the
  `(ref: ...)` upstream ref out of the response body).
- `httpx.HTTPError` or subclass — exhausted retries on transport
  errors.

If you genuinely need to bypass the breaker (e.g. a one-shot health
check that should NOT count toward breaker failures), construct your
own `httpx.AsyncClient` — but write a CLAUDE.md note explaining why.
The default is "go through `upstream_http`."

### Mocking outbound HTTP — `install_fake_httpx_ollama` fixture

The shared helper in `web/backend/tests/conftest.py` mocks at the
**transport layer** of the `upstream_http` singleton (via
`httpx.MockTransport`), so all the production retry / breaker / timeout
wiring stays exercised but transport responses are deterministic:

```python
from .conftest import install_fake_httpx_ollama

def test_something(monkeypatch):
    record = install_fake_httpx_ollama(
        monkeypatch,
        ids=["gpt-oss:120b", "qwen3-coder:480b"],
        # OR raise_exc=httpx.ConnectError("boom") for failure-mode tests
        # OR status=401 to simulate auth failure
    )
    # ... exercise the catalog/health/runs endpoint ...
    assert record["calls"] == 1
    # Note: MockTransport normalises header names to lowercase
    # (HTTP/1.1 RFC 7230 §3.2 — case-insensitive). The legacy stub
    # preserved case; tests should check both.
    auth = (record["last_headers"].get("authorization")
            or record["last_headers"].get("Authorization"))
    assert "Bearer" in auth
```

`record` is a dict tracking `{"calls", "last_url", "last_headers"}` so
tests can assert that the right URL and auth headers were sent.
Combined with the autouse `_reset_ollama_cache` fixture (which calls
`ollama_models._reset_for_tests()` → also resets the
`upstream_http` singleton + circuit breaker), every test starts from
a clean state.

The two service-level test files
(`test_ollama_models_service.py` and `test_ollama_models_failure_keeps_last_good.py`)
keep their own local helper because they need full control of the
response JSON to test malformed-item edge cases and multi-step
success-then-failure scripts — but they also install a
`MockTransport` on `upstream_http._client` rather than monkeypatching
`httpx.AsyncClient` directly. Router-level tests should always use
the shared helper.

## The soft-auth pattern

For routers that ship before `app/auth.py` lands, or for tests that
need to mount a router in isolation, use this defensive import:

```python
try:
    from app.auth import get_current_user
except ImportError:
    from app.schemas import AuthUser
    def get_current_user() -> AuthUser:
        return AuthUser(username="anonymous")
```

The real dependency replaces the stub the moment `app.auth` exists.
This pattern unblocks parallel-agent work and is harmless in
production where auth is always present.

## Security invariants (always)

These rules are non-negotiable. Tests guard them; CLAUDE.md echoes
them; the parent CLAUDE.md adds the broader context.

| Rule | Why | Reference |
|---|---|---|
| Ticker → filesystem path goes through `safe_ticker_component` | Tickers come from user input AND prompt-injectable LLM tool calls. The function whitelists `[A-Za-z0-9._\-\^]+` (caret needed for `^GSPC`-style indexes), rejects all-dots / over-long inputs. | `tradingagents/dataflows/utils.py` |
| API keys are Fernet-encrypted at rest | Database compromise must not leak provider credentials. | `app/crypto.py`, `app/models.py:ApiKey` |
| Plaintext API keys never appear in responses, logs, or error messages | Including `GET /api/settings/api-keys` (returns `{configured, last_updated}` only). | `app/routers/settings.py` |
| CSRF exempts exactly `POST /api/auth/login` | Chicken-and-egg: login sets the CSRF cookie. Every other state-changing path requires the double-submit header. | `app/middleware/csrf.py` |
| State-changing routes require JWT | Including logout (defense-in-depth). | `app/routers/auth.py` |

## The `GLOBAL_RUN_LOCK` invariant

`app/services/run_service.py:GLOBAL_RUN_LOCK` (an `asyncio.Lock`) is
held for the entire window that `env_inject.scope(api_keys)` is
mutating `os.environ`. v1 is single-concurrent-run by design.

**Don't add concurrent-run support without first migrating off
`env_inject.scope`** to per-call key injection through
`create_llm_client(..., api_key=...)`. The env-var path is only safe
because nothing else is touching `os.environ` while the engine runs.

## Reuse, don't duplicate

The parent `tradingagents` package owns the engine, the provider
catalog, and the security helpers. The Web UI should import, not
re-derive.

| Need | Reuse from |
|---|---|
| Provider list (display name, default base URL, regions) | `tradingagents.providers.PROVIDERS` |
| Provider list filtered to "credentials present in env" | `tradingagents.providers.available_providers()` |
| Per-provider model lists (static, non-Ollama) | `tradingagents.llm_clients.model_catalog.MODEL_OPTIONS` |
| Live Ollama / Ollama Cloud model discovery (TTL-cached, never-raises) | `app.services.ollama_models.list_ollama_models()` |
| Most-recent Ollama upstream probe outcome (`ok` / `down` / `unknown`) | `app.services.ollama_models.last_probe_status()` |
| Provider → env-var name mapping | `tradingagents.llm_clients.api_key_env.PROVIDER_API_KEY_ENV` |
| Asset-type detection | `tradingagents.asset_types.detect_asset_type(ticker)` |
| Analyst filter for asset type | `tradingagents.asset_types.filter_analysts_for_asset_type` |
| Run-the-graph chunk loop | `tradingagents.run_observer.stream_run(...)` |
| Abstract observer base class | `tradingagents.run_observer.RunObserver` |
| Engine's analyst display names | `tradingagents.run_observer.ANALYST_AGENT_NAMES` |
| Analyst → report-section key map | `tradingagents.run_observer.ANALYST_REPORT_MAP` |
| Ticker filesystem safety | `tradingagents.dataflows.utils.safe_ticker_component` |
| 5-tier rating values | `tradingagents.agents.utils.rating.RATINGS_5_TIER` |
| Pydantic event shapes | `app.schemas.RunEvent` (discriminated union) |
| Fernet encrypt/decrypt | `app.crypto.{encrypt, decrypt, reset_cache}` |

## Database changes

If you need a new table or column:

1. Add the SQLAlchemy mapping in `app/models.py`.
2. Generate a new Alembic revision:
   ```bash
   cd web/backend
   alembic revision --autogenerate -m "describe the change"
   ```
3. Review the generated `alembic/versions/*.py`. Autogenerate misses
   some things (server defaults, partial indexes, constraint names).
   The existing `0001_initial.py` is the reference for cross-dialect
   `with_variant(...)` patterns (JSONB on Postgres, JSON elsewhere).
4. `alembic upgrade head` to apply locally.
5. Update the matching Pydantic schema in `app/schemas.py` AND the
   TypeScript mirror in `web/frontend/src/lib/types.ts`. The backend
   and frontend types must stay in sync.

## Mirror the existing routers

When unsure how to structure a new router, copy the pattern from one
that already does something similar:

- Simple read endpoint with auth → `app/routers/health.py`
- Read endpoint with query filters → `app/routers/history.py`
- Read + write with at-rest encryption → `app/routers/settings.py`
- Submit-then-stream lifecycle + SSE → `app/routers/runs.py`
- Proxy with caching → `app/routers/announcements.py`

Each of those is short, focused, and uses the established conventions.
