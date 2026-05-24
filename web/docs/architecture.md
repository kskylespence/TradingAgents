# Architecture

> Audience: anyone new to the Web UI subtree. Read this first.

## The 10,000-foot view

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser (Vite/React SPA)                                          │
│  /login   /new   /runs/:id   /history   /settings                  │
└──────────┬────────────────────────────────────┬────────────────────┘
           │ POST /api/* (JSON, JWT cookie)     │ GET /api/runs/:id/events (SSE, heartbeats)
           ▼                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│  FastAPI app (uvicorn)                                             │
│                                                                    │
│   middleware/        security_headers · csrf                       │
│   routers/           auth · catalog · runs · history · settings ·  │
│                      health · announcements                        │
│   lifespan_hooks/    crash_recovery · disk_pruner                  │
│   services/          run_service · event_bus · env_inject ·        │
│                      rate_limit · announcements · disk_pruner ·    │
│                      crash_recovery                                │
│   observers/         WebRunObserver                                │
│   auth.py · catalog.py · crypto.py · config.py · db.py · models.py │
│   schemas.py · logging_config.py · main.py                         │
└──────────┬──────────────────────────────┬──────────────────────────┘
           │                              │
           ▼                              ▼
┌──────────────────────────┐   ┌──────────────────────────────────┐
│  TradingAgentsGraph      │   │  Neon Postgres (prod) / SQLite   │
│  (the existing engine,   │   │  runs · run_events · api_keys ·  │
│   checkpoint-enabled for │   │  user_defaults · login_attempts  │
│   crash resume)          │   └──────────────────────────────────┘
└──────────┬───────────────┘
           ▼
   /data/tradingagents/   (Coolify persistent volume)
   ├── logs/<ticker>/     reports + JSON intermediates
   ├── cache/checkpoints/ LangGraph SQLite checkpoint files
   └── memory/            trading_memory.md (cross-run decision log)
```

The Vite build is copied into `web/backend/app/static/` at image-build
time. FastAPI mounts that directory at `/` as a static catch-all, so
the same image serves both `/api/*` and the SPA. In local dev, Vite's
dev server runs separately on `:5173` and proxies `/api` to `:8000`.

## Why this shape

- **One image, one engine.** Both the CLI (`../cli/main.py`) and this
  Web UI call into the same
  `TradingAgentsGraph(...).propagate(ticker, date)` entry point. There
  is no behavior fork — when the engine changes, both surfaces see it.
- **Postgres for metadata, filesystem for blobs.** Reports are
  50–200 KB markdown files; we don't need them in SQL. The `runs`
  table is small and indexable, and the `run_events` table is the
  append-only source-of-truth for the SSE replay.
- **SSE not WebSocket.** One-way streaming from server to browser is
  exactly what a run is. SSE has built-in `Last-Event-ID` resume, no
  separate socket protocol to maintain, and works through any reverse
  proxy that handles HTTP/1.1 chunked responses (Coolify's Traefik
  does).
- **Single concurrent run in v1.** A global `asyncio.Lock` serializes
  runs so `env_inject.scope(api_keys)` can mutate `os.environ` safely
  while the engine is running. Concurrent runs would require migrating
  to per-call `create_llm_client(..., api_key=...)` injection.
- **Auto-discovery registries.** Routers, middleware, and lifespan
  hooks each have a `_autoload()` in their package `__init__.py` that
  imports every non-underscore submodule. Dropping a new file IS the
  wiring — `main.py` doesn't need to know. This is what let five
  waves of parallel agents finish the build without touching shared
  code. See [`backend-dev.md`](backend-dev.md).

## The five pluggable surfaces

| Surface | Pattern | Module |
|---|---|---|
| HTTP route | `register(router)` at module scope | `app/routers/` |
| Request middleware | `register(install)` at module scope | `app/middleware/` |
| App lifecycle | `@on_startup` / `@on_shutdown` decorators | `app/lifespan_hooks/` |
| Run lifecycle observer | Subclass `RunObserver` from the parent package | `app/observers/` |
| Backend service / library | Just a file, no registration | `app/services/` |

Drop a file in the right place and it wires itself.

### Notable service modules

- `services/event_bus.py` — per-run SSE pub/sub.
- `services/run_service.py` — the full run lifecycle (lock, env-inject, engine, terminal-state writeback).
- `services/rate_limit.py` — login lockout, persisted to `login_attempts`.
- `services/disk_pruner.py` — background retention sweep for reports and checkpoints.
- `services/crash_recovery.py` — on-boot pass that flips orphan `running` runs to `interrupted`.
- `services/upstream_http.py` (**0.2.5+hf.4**) — shared resilient HTTP client for every outbound call to Ollama. One module-singleton `httpx.AsyncClient` with HTTP/2, `Limits()`, and `Timeout(connect=10, read=15, write=10, pool=10)`. Layered with `tenacity` retry (exponential jitter on transient transport errors + 429/5xx), `Retry-After` header honouring (integer-seconds OR HTTP-date per RFC 7231 §7.1.3), and a `circuitbreaker.CircuitBreaker` that opens after 5 consecutive failures and half-opens after 30s. Public surface: `request(method, url, ...)`, `circuit_state()`, `close_client()`. Built specifically to absorb Ollama Cloud's documented instability (`ollama/ollama#14673`, `#15419`, `#15910`, `#13770`) without surfacing transients to the user. The run-time LLM call path (`tradingagents.llm_clients.openai_client`) does NOT go through this — it has its own SDK-level retry budget; this module is dedicated to the catalog/health/probe seam.
- `services/ollama_models.py` — live discovery of Ollama / Ollama Cloud models via `GET {OLLAMA_BASE_URL}/models`, with 5-minute TTL cache and never-raises contract. Routes through `upstream_http.request`. State per `base_url`: `_cache` (only updated on success — the catalog's last-good fallback) and `_last_attempts` (a `deque[3]` of attempt outcomes driving the 2-of-3 hysteresis on `last_probe_status()` so single transients don't flap). `list_ollama_models()` uses stale-while-revalidate: on cache expiry it returns the stale list immediately and schedules a background refresh task. Also exposes `recent_attempts()` for the health endpoint and `probe_model_liveness(model_id)` for the pre-flight probe (Layer 1).
- `lifespan_hooks/upstream_warmup.py` (**0.2.5+hf.4**) — on startup, spawns a fire-and-forget `list_ollama_models()` call (20s timeout) so the singleton client's DNS+TLS handshake is paid before the first user request. Also spawns a refresh loop every 240s (just before the 5-min cache TTL) so the cache never goes cold. On shutdown: cancels BOTH the warmup and refresh tasks, then `upstream_http.close_client()`.

## Run lifecycle walkthrough

What happens between the user clicking **Submit** and the
`<DecisionBadge>` rendering on the run page:

1. **`POST /api/runs`** (`routers/runs.py`) → validates the
   `RunRequest`, calls `run_service.start_run(req, db)`.
2. **`start_run`** mints a UUID, INSERTs a `runs` row with
   `status='queued'`, schedules `asyncio.create_task(_run_async(...))`,
   returns the `run_id` to the client immediately.
3. **`_run_async`** acquires `GLOBAL_RUN_LOCK` (HTTP 409 to anyone
   else who tries to start a run while we hold it). Decrypts the
   provider API keys we need from the `api_keys` table.
4. **`env_inject.scope(api_keys)`** snapshots the relevant
   `os.environ` entries, applies the decrypted keys, and guarantees
   restoration on exit (including `CancelledError`).
5. **`WebRunObserver`** is constructed with a `publish` callable
   bound to this run's id. It subclasses the parent package's
   `RunObserver` (see `tradingagents/run_observer.py`).
6. The graph is built, the run row flips to `running`, a
   `run_started` event is published, then the synchronous engine loop
   runs on a worker thread via
   `asyncio.to_thread(stream_run, graph, init_state, args, observer,
   cancel_event=cancel_event)`.
7. As the engine streams `for chunk in graph.stream(...)`, each chunk
   triggers `RunObserver` callbacks. `WebRunObserver` converts each
   callback to a Pydantic `RunEvent` and hands it to a thread-safe
   bridge that schedules `event_bus.publish(run_id, payload)` on the
   asyncio loop.
8. **`event_bus.publish`** INSERTs into `run_events` (with a
   monotonic per-run `seq`, serialized by a lazy per-run
   `asyncio.Lock`) **before** fan-out. Then it `put_nowait`s onto each
   subscriber's queue. If a queue is full (size 200), the live frame
   is dropped — the database row remains, so a slow client catches up
   via `Last-Event-ID` replay on reconnect.
9. **SSE clients** subscribed via `GET /api/runs/:id/events` see the
   events as they're published. `sse-starlette` sends `: heartbeat`
   comments every 15 seconds so any reverse proxy's idle-connection
   timeout doesn't close the stream.
10. When the engine returns, the run row flips to `completed`,
    `signal_processor.process_signal(...)` parses the rating, the
    report is materialized to
    `<data_dir>/logs/<safe_ticker>/<date>/reports/`, and
    `run_completed` is published with the rating + report dir.
11. The observer's `aclose()` drains every in-flight publish (so SSE
    clients see the final event before the stream closes), then
    `event_bus.close(run_id)` flips the close flag and subscribers
    exit on their next read.

If anything raises in step 6: `run_failed` is published, the row flips
to `failed`. If the cancellation event is set (via
`POST /api/runs/:id/cancel`): `run_cancelled` is published instead.

Before an exception can reach step 6, it has to escape **three layers of
retry/safety**:

1. **OpenAI SDK transport retries**, configured by
   `tradingagents.llm_clients.openai_client.OpenAIClient.get_llm()` —
   5 retries for cloud providers, 2 for native openai, exponential
   backoff with jitter (~32 s total envelope for cloud). Catches
   transient `5xx` from the HTTP layer before they become Python
   exceptions.
2. **LangGraph node retries**, configured in
   `tradingagents.graph.trading_graph._TRANSIENT_RETRY_POLICY` and
   stamped on every node via `_attach_retry_policy(workflow)`. 3 total
   attempts (initial + 2 retries) with 8 s / 16 s backoff. Catches
   `openai.InternalServerError`, `openai.APITimeoutError`,
   `openai.APIConnectionError`, and `httpx.RemoteProtocolError` that
   still escape the SDK layer.
3. **Per-run wall-clock timeout** (`run_service._run_async`, **0.2.5+hf.4**).
   The engine call is wrapped in
   `asyncio.wait_for(_run_engine(...), timeout=TRADINGAGENTS_RUN_MAX_SECONDS)`
   (default 1800 s / 30 min). On `TimeoutError` the cooperative
   `cancel_event` is set so the worker thread stops at the next chunk
   boundary, the run is marked `failed` with a clear error naming the
   env var, and the existing `finally:` block releases
   `GLOBAL_RUN_LOCK`. This is the outer safety net that bounds the
   single-concurrent-run lock's hold time even if every other layer
   fails — without it, a hung LLM call held the lock indefinitely
   (the 2026-05-22 56-minute hang in `2ccfeda` could have blocked
   every subsequent run).

Only an exception that survives ALL THREE layers turns into a `failed`
status. The persisted `error_message` is the human-readable string
from
`web/backend/app/services/run_service.py:_format_engine_error(exc, provider)`
— the helper names the provider, surfaces any upstream `ref:`
correlation ID, and suggests a next action per exception class.

### Catalog/health probe resilience (parallel pipeline)

The `/api/health` poll (every 30s from the frontend) and the
`/api/catalog/models?provider=ollama` fetch share a separate resilience
pipeline owned by `services/upstream_http.py`. The matrix:

| Layer | Lives in | Triggers on |
|---|---|---|
| Retry on transients | `upstream_http.request` (tenacity) | `ConnectError`/`Timeout`, `ReadTimeout`, `WriteTimeout`, `RemoteProtocolError`, `PoolTimeout`, plus 429 / 5xx wrapped as `RetryableStatusError` |
| Retry-After honouring | same | 429 / 5xx response with the header (integer-seconds or HTTP-date) |
| Circuit breaker | same (`circuitbreaker.CircuitBreaker`) | 5 consecutive failures; opens for 30s; half-opens for one trial probe before closing |
| Hysteresis on user-visible state | `ollama_models.last_probe_status()` | reports `"down"` only when 2-of-3 recent attempts failed |
| Stale-while-revalidate | `ollama_models.list_ollama_models()` | cache expiry returns stale immediately + spawns background refresh |
| Startup warmup + 4-min refresh | `lifespan_hooks/upstream_warmup.py` | pre-resolves DNS+TLS on boot; keeps cache warm |

Layered logging at every transition (`upstream_http.retry_attempt`,
`retry_after_honored`, `circuit_opened`/`half_open_probe`/`closed`,
`ollama_models.health_transition`, `ollama_models.stale_served`)
makes the production failure path grep-friendly.

## Crash-recovery contract

Two layers:

- **Database-level.** Every `RunEvent` is in Postgres **before** it
  hits any in-memory queue. A server crash loses no recorded progress;
  any client can replay the full event stream with `Last-Event-ID: 0`.
- **Engine-level.** When `enable_checkpoint=True` is requested on a
  run, the graph is compiled with the LangGraph SQLite checkpointer
  keyed on `thread_id(ticker, date)` (see
  `tradingagents/graph/checkpointer.py`). The checkpoint DB lives at
  `<data_dir>/cache/checkpoints/<TICKER_UPPER>.db`.

On startup, `services/crash_recovery.run_startup_recovery(db)`:

1. SELECTs `runs` rows with `status='running'` (orphans from a crash).
2. For each, appends a terminal `run_failed` event so reconnecting SSE
   clients see a stream end, then flips the row to `interrupted`.
3. Sets `resumable=True` iff `checkpoint_enabled=True` AND
   `has_checkpoint(ticker, date)` finds the file on disk.

The frontend reads `resumable` from `GET /api/runs/:id` and shows a
**Resume** button. Clicking it calls
`POST /api/runs/:id/resume`, which creates a NEW run row with the
same `(ticker, date)` so the LangGraph thread_id collides → the
engine picks up from the checkpoint. The response is
`{run_id, parent_run_id}` and the frontend navigates to the new run.

For `failed` and `cancelled` runs (which have no resumable
checkpoint — either it never landed or `checkpoint_enabled` was
`False`), the frontend shows a **Retry** button instead.
`POST /api/runs/:id/retry` reconstructs a fresh `RunRequest` from the
parent's persisted columns and submits it as a brand-new run — the
graph starts from scratch, not from a checkpoint. Same
`{run_id, parent_run_id}` response shape as `/resume`. See
`web/docs/api.md` "Retry contract" for the violation matrix.

## Persistence at a glance

| What | Where | Lifecycle |
|---|---|---|
| Run metadata | Postgres `runs` | One row per run, never deleted (history queries) |
| Event log | Postgres `run_events` | One row per published event, cascade-deletes with the run |
| API keys | Postgres `api_keys` | One row per provider env-var, Fernet-encrypted |
| User defaults | Postgres `user_defaults` | Singleton (id=1) |
| Login attempts | Postgres `login_attempts` | Append-only, used to persist rate-limit lockout across restart |
| Reports | `<data_dir>/logs/<ticker>/<date>/reports/` | One dir per run, pruned by `disk_pruner` after `RETENTION_DAYS` |
| Checkpoints | `<data_dir>/cache/checkpoints/<TICKER>.db` | One file per `(ticker, date)`, pruned with reports |
| Memory log | `<data_dir>/memory/trading_memory.md` | Append-only cross-run decision log |

## Next reads

- [`backend-dev.md`](backend-dev.md) — patterns for extending the FastAPI app
- [`frontend-dev.md`](frontend-dev.md) — patterns for extending the SPA
- [`api.md`](api.md) — endpoint reference + SSE contract
- [`operations.md`](operations.md) — env vars + runbook
