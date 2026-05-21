# CLAUDE.md — `web/backend/`

Authoritative conventions for Claude Code sessions editing under
`web/backend/`. Inherits from the repo-root `CLAUDE.md`; this file
overrides anything more general when the two conflict.

## What this subtree is

A FastAPI application that wraps `TradingAgentsGraph` (the engine that
lives in the parent `tradingagents` package). Single-user, login-gated,
SSE-streamed, designed to deploy as one Docker image to Coolify.

## Extension surface — auto-discovery registries

| Add this | Drop a file here | Register with | Ordering |
|---|---|---|---|
| HTTP router | `app/routers/<name>.py` | `from . import register; register(router)` at module scope | Inclusion order (later-included shadows nothing if paths differ) |
| HTTP middleware | `app/middleware/<name>.py` | `from . import register; register(install)` where `install(app: FastAPI)` calls `app.add_middleware(...)` | **REVERSE registration** — last `register(install)` wraps the request first |
| Startup / shutdown hook | `app/lifespan_hooks/<name>.py` | `@on_startup` / `@on_shutdown` decorators on `async def fn(app)` | Startup in registration order; shutdown in reverse |
| Service module | `app/services/<name>.py` | No registration — just a file | n/a |
| Observer | `app/observers/<name>.py` | No registration — instantiated by `run_service` | n/a |

Long version in `web/docs/backend-dev.md`. `_autoload()` in each registry's
`__init__.py` imports every non-underscore submodule on package load, so
dropping a file IS the wiring. **Do not edit `main.py` to wire a new
router/middleware/hook — that defeats the registry pattern.**

## Files you must NOT modify casually

Foundation files. Parallel agents rely on these being stable.

- `app/__init__.py`, `app/main.py` (factory + lifespan)
- `app/config.py` (pydantic-settings)
- `app/db.py` (engine + session factory)
- `app/models.py` (5 ORM tables — schema changes require a new Alembic revision)
- `app/schemas.py` (Pydantic — backend↔frontend contract; mirror change in `web/frontend/src/lib/types.ts`)
- `app/crypto.py` (Fernet wrappers)
- `app/logging_config.py`
- The three registry `__init__.py` files: `app/routers/__init__.py`,
  `app/middleware/__init__.py`, `app/lifespan_hooks/__init__.py`
- `alembic/env.py` and every file under `alembic/versions/`

Changes to any of these need a clear reason in the commit message.

## Security invariants (always)

- **Ticker → filesystem path** MUST go through
  `tradingagents.dataflows.utils.safe_ticker_component`. Tickers come
  from user input AND from LLM tool calls (prompt-injectable). The
  function whitelists `[A-Za-z0-9._\-\^]+` and rejects all-dots / over-
  long inputs. Current callsites: `services/run_service.py` (report dir),
  `services/disk_pruner.py`, `services/crash_recovery.py`.
- **API keys** go through `app.crypto.{encrypt,decrypt}`. Never store
  plaintext. Never return plaintext (not in responses, not in error
  bodies, not in logs).
- **CSRF** exempts exactly `POST /api/auth/login` (the chicken-and-egg
  path that *sets* the CSRF cookie). Any new exemption needs a
  documented threat-model reason in `middleware/csrf.py`.
- **JWT required on state-changing routes** — including logout. The
  `Depends(get_current_user)` on logout is defense-in-depth.

## Async-loop invariants

- **Module-level `asyncio.Lock()` is forbidden.** Use the lazy
  `_get_lock()` pattern from `services/event_bus.py` and
  `services/rate_limit.py`:
  ```python
  _lock: asyncio.Lock | None = None
  def _get_lock() -> asyncio.Lock:
      global _lock
      if _lock is None:
          _lock = asyncio.Lock()
      return _lock
  ```
  Reason: a Lock created at import time binds to whichever loop is
  current then. Under uvicorn `--reload` and under pytest-asyncio (which
  gives each test its own loop) that loop is dead by the time you await
  the lock → `RuntimeError: <Lock> is bound to a different event loop`.
  Any `reset_for_tests()` / `reset()` helper MUST also set the lock back
  to `None`.
- **`GLOBAL_RUN_LOCK`** in `services/run_service.py` is held for the
  entire window `env_inject.scope(api_keys)` mutates `os.environ`. v1 is
  single-concurrent-run. Don't add concurrency without first migrating
  off `env_inject.scope` to per-call `create_llm_client(..., api_key=...)`
  injection.

## Routing pitfall

FastAPI matches routes in **first-registered-wins** order. Anything
registered inline on `app` in `main.py` (e.g. `@app.get("/api/health")`)
will shadow auto-discovered routers mounted at the same path. The
`/api/_bootstrap_health` placeholder in `main.py` is the one legitimate
inline route (it's the router-import-failure fallback). Don't add more
without a comparable reason.

## Testing conventions

- Mirror `tests/test_foundation_smoke.py`. `tests/conftest.py` already
  provides autouse dummy provider keys + an in-memory SQLite engine + a
  per-test `db_session` async fixture.
- **Soft-auth in tests** —
  `app.dependency_overrides[get_current_user] = lambda: AuthUser(username="test")`.
- **CSRF disable in tests** — monkeypatch
  `app.middleware.csrf._csrf_required` to return `False`, OR set the
  cookie/header pair on the request.
- **SQLite + UUID gotcha** — aiosqlite cannot bind a raw `uuid.UUID` to
  the `String(36).with_variant(UUID, "postgresql")` column. Coerce to
  `str(uuid_instance)` before insertion. References: `test_history.py`,
  `test_runs_smoke.py::test_resume_happy_path_returns_new_run_id`.
- **FAKE_LLM=1** short-circuits `run_service._run_engine` to a scripted
  observer-driven simulator (~0.3s, always returns Buy). Use this
  instead of monkeypatching `stream_run` for end-to-end tests.
- **Module-level locks must be reset per test** —
  `event_bus._lock = asyncio.Lock()` (or call `reset_for_tests()`),
  `login_rate_limiter.reset()`. pytest-asyncio gives each test its own
  loop and stale locks crash.

## Shared modules — reuse, don't duplicate

| Need | Reuse |
|---|---|
| Provider list | `tradingagents.providers.PROVIDERS` |
| Models per provider | `tradingagents.llm_clients.model_catalog.MODEL_OPTIONS` |
| Provider → env var | `tradingagents.llm_clients.api_key_env.PROVIDER_API_KEY_ENV` |
| Asset-type detection | `tradingagents.asset_types.{detect_asset_type, filter_analysts_for_asset_type, CRYPTO_SUFFIXES}` |
| Stream a graph run | `tradingagents.run_observer.{RunObserver, stream_run, ANALYST_AGENT_NAMES, ANALYST_REPORT_MAP}` |
| Filesystem ticker safety | `tradingagents.dataflows.utils.safe_ticker_component` |
| Pydantic event shapes | `app.schemas.{RunEvent, RunStartedEvent, AgentStatusEvent, ...}` (full discriminated union) |
| Encryption | `app.crypto.{encrypt, decrypt, reset_cache}` |

## Where the long-form docs live

- `web/docs/backend-dev.md` — adding routers/middleware/hooks, with examples
- `web/docs/architecture.md` — how the runner + event bus + observer fit together
- `web/docs/api.md` — endpoint reference, SSE event taxonomy, auth flow
- `web/docs/testing.md` — pytest + FAKE_LLM + soft-auth + red-green discipline
- `web/docs/operations.md` — env vars, secret rotation, health states
- `web/backend/dev-install.md` — pip / uv install dance
