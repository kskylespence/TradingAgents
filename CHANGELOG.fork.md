# Changelog (fork)

All notable changes to **[`kskylespence/TradingAgents`](https://github.com/kskylespence/TradingAgents)**
beyond the upstream baseline are documented here. For changes that came
in from upstream `TauricResearch/TradingAgents`, see the sibling
[`CHANGELOG.md`](./CHANGELOG.md) — that file is the unchanged upstream
mirror and is not edited by this fork.

Format: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versioning: PEP 440 local segment `<upstream-version>+hf.<N>` (e.g.
`0.2.5+hf.1`). The `+hf.N` counter resets to `1` whenever the upstream
base version moves forward. See [`docs/RELEASING.md`](./docs/RELEASING.md)
for the per-deploy cut workflow.

## [Unreleased]

### Added

- **Multi-user profiles (web).** Database-backed `users` table with `admin` and
  `user` roles, per-user run/history isolation, JWT claims (`id`, `username`,
  `role`), startup bootstrap of the env-configured admin account, and optional
  `rob@rob` seeding via `ROB_INITIAL_PASSWORD`. Regular users cannot access
  Settings or choose LLM provider/models — those values come from admin defaults
  enforced server-side on `POST /api/runs`.

- **Trading-terminal UI (web).** Dark default theme, HedgeFund Terminal branding,
  market color tokens, Final Trade Decision pinned at the top of run reports
  plus a hero card on the run view, and role-gated hiding of provider/model
  metadata for non-admin users.

### Changed

- **Report ordering (web).** `final_trade_decision` is now the first section in
  `ReportPanel` instead of last.

### Fixed

- **Orphaned catalog refreshes on shutdown (web).** `ollama_models` schedules
  stale-while-revalidate refreshes with a fire-and-forget `asyncio.create_task`,
  and nothing ever awaited them. `shutdown_ollama` cancelled the two tasks it
  owns but not these, so a restart mid-refresh closed the event loop on a live
  task — asyncio logged `Task was destroyed but it is pending!` and the HTTP
  request was abandoned, while `upstream_http.close_client()` pulled the
  connection pool out from under it. New `drain_in_flight_refreshes()` settles
  them (bounded wait, then cancel-and-await) and the shutdown hook calls it
  *before* closing the client. Note `task.cancel()` alone was never sufficient:
  it only requests cancellation, leaving the task in state `cancelling` until
  the loop runs it again.

- **Backend test teardown (web).** The autouse `_reset_ollama_cache` fixture is
  now `async` so it can *await* the drain above. As a sync fixture it could
  only call `task.cancel()`, and pytest-asyncio closed the per-test loop before
  the cancellation was processed — surfacing as two `ERROR at teardown` /
  `RuntimeError: Event loop is closed` in
  `test_ollama_models_failure_keeps_last_good.py`.

- **`[tool.uv.sources]` path (web backend).** `tradingagents` pointed at `..`,
  which resolves relative to `web/backend/` and therefore at `web/` — a
  directory with no `pyproject.toml`. Every uv-based install failed with "does
  not appear to be a Python project", including the `uv sync --extra dev` route
  that `dev-install.md` documents. Corrected to `../..`. Plain pip was immune
  because it ignores `[tool.uv.sources]` entirely, which is why this went
  unnoticed.

- **Unpinned test dependencies (web backend).** `pytest-asyncio` is now capped
  `<1.0`: the 1.x per-test loop and fixture-finalisation changes break
  `test_rate_limit.py` ("There is no current event loop in thread 'MainThread'")
  and three `test_runs_preflight_probe.py` tests, so an uncapped `>=0.24` let a
  fresh install silently resolve to 1.x and drop the suite from all-green to
  3 failed / 5 errors. Also added an explicit `psycopg[binary]` dev dep —
  `pytest-postgresql` auto-loads via entry points and imports `psycopg` at
  collection time, so on a host with no system libpq (macOS without Homebrew)
  the entire session aborted before running a single test.

- **Migration 0003 (Postgres).** Cast bootstrap admin UUID literals in raw SQL so
  `alembic upgrade head` succeeds on PostgreSQL (asyncpg rejected varchar binds
  against UUID columns).

- **CI (upstream gate).** Register the `asyncio` pytest marker, skip
  `test_run_service_error_format` when FastAPI is not installed, re-export
  `detect_asset_type` from `cli.utils`, classify bare `BTCUSD`/`ETHUSD` as
  crypto, and bring `web/backend` in line with strict `ruff check .` (plus
  exclude `web/backend/build` artifacts).

## [0.3.1+hf.1] — 2026-07-08

### Added

- **Upstream v0.3.1 merge.** Integrated `TauricResearch/TradingAgents`
  v0.3.0–v0.3.1: debate/risk router crash-safety (#1088), checkpoint thread
  identity (#1089), verified data-access contract, provider registry (Bedrock
  bearer auth, NIM/Kimi/Groq/Mistral/openai_compatible), FRED + Polymarket
  vendors, nullish-float structured-output coercion (#1058), and upstream CI
  gate.

- **Run stats wiring (web).** `run_service` passes `AnalystWallTimeTracker`
  + `StatsCallbackHandler` into the engine so `runs.stats` persists token
  counts and per-analyst wall times. `StatsCallbackHandler` lives in
  `tradingagents/stats_handler.py`.

- **Postgres password rotation helper (`web/backend/scripts/rotate_db_password.py`).**
  One-shot `ROTATE_DB_PASSWORD=… python …/rotate_db_password.py` for
  Coolify operators replacing the initial `PLACEHOLDER` password.

### Changed

- **Deployment and README docs.** [`DEPLOY.md`](DEPLOY.md) documents
  Coolify Postgres, GHCR prebuild, lite preset, and `glm-5.2` defaults;
  operations guide covers env deduplication and DB password rotation.

### Fixed

- **FastAPI `Query` deprecation.** `GET /api/runs/{id}/report` uses
  `pattern=` instead of deprecated `regex=`.

## [0.2.5+hf.7] — 2026-07-08

### Changed

- **Default Ollama model (`glm-5.2`).** `UserDefaults` now ships
  `llm_provider=ollama` with `quick_think_llm` and `deep_think_llm` both
  set to `glm-5.2` so fresh installs and the New Run form pre-select the
  current GLM flagship. Added `glm-5.2` to the curated cloud snapshot;
  probe-failure alternatives pin the newest GLM headline first.

## [0.2.5+hf.6] — 2026-07-08

### Fixed

- **`httpx[http2]` dependency.** The shared `upstream_http` client enables
  HTTP/2 for Ollama Cloud multiplexing, but the Docker image only installed
  bare `httpx` — missing the `h2` extra caused every Ollama probe and model
  catalog fetch to fail with `ImportError` on production deploys. Both
  `pyproject.toml` files now declare `httpx[http2]`.

## [0.2.5+hf.5] — 2026-07-08

### Added

- **GHCR publish workflow (`.github/workflows/docker-publish.yml`).** Builds
  and pushes the Coolify image to `ghcr.io/kskylespence/tradingagents` on
  every `v*-hf.*` tag (and via manual dispatch) so small VPS hosts can pull
  a prebuilt image instead of running `npm ci` + dual `pip install` on-box
  during deploy. Documented in [`DEPLOY.md`](./DEPLOY.md) § Prebuilt image.

- **Lite VPS operator docs.** [`DEPLOY.md`](./DEPLOY.md) now documents minimum
  VPS sizing, swap for deploy builds, GHCR pull workflow, and OOM/CPU
  triage. [`web/docs/operations.md`](./web/docs/operations.md) adds a
  **Lite VPS preset** env block and **VPS troubleshooting** table (deploy
  vs first-run correlation). [`.env.example`](./.env.example) mirrors the
  preset.

- **Upstream merge tracker ([`docs/UPSTREAM-MERGE.md`](./docs/UPSTREAM-MERGE.md)).**
  Schedules the v0.3.1 merge as a dedicated task — stability fixes, not a
  CPU emergency.

- **Lite form defaults (`UserDefaults`).** Fresh installs now return
  `research_depth=1` and `analysts=["market","social"]` from
  `GET /api/settings/defaults` until the user saves different choices,
  reducing first-run LLM call volume on small hosts.

### Changed

- **Ollama warmup gated (`upstream_warmup`).** The lifespan hook no-ops
  unless `OLLAMA_BASE_URL` is explicitly set and Ollama is the active
  provider or appears in `available_providers()`. OpenAI-only VPS deploys
  no longer probe `localhost:11434` on a 4-minute background loop.
  Tests: `web/backend/tests/test_lifespan_upstream_warmup.py` (+2).

## [0.2.5+hf.4] — 2026-05-24

### Added

  outbound call to Ollama (catalog list + per-model liveness probe) now
  routes through a single singleton `httpx.AsyncClient` wired with
  HTTP/2, `Limits(max_keepalive=10, max_connections=20,
  keepalive_expiry=30s)`, and `Timeout(connect=10, read=15, write=10,
  pool=10)`. Three resilience layers wrap it:
  (1) **tenacity** retries on transient transport errors
  (`ConnectError`, `ConnectTimeout`, `ReadTimeout`, `WriteTimeout`,
  `RemoteProtocolError`, `PoolTimeout`) and on 429 / 5xx responses,
  with `wait_exponential_jitter(initial=0.5, max=8.0)`,
  `stop_after_attempt(3) | stop_after_delay(25s)`;
  (2) **`Retry-After` header honouring** — both integer-seconds and
  HTTP-date forms (RFC 7231 §7.1.3) are parsed and slept verbatim
  before the next retry, capped at 30s, so we obey Ollama Cloud's own
  backpressure signal instead of amplifying it;
  (3) **circuit breaker** (`circuitbreaker.CircuitBreaker`) that opens
  after 5 consecutive failures, stays open for 30s, then half-opens
  for one trial probe before closing. The motivation is the documented
  Ollama Cloud instability (`ollama/ollama#14673` Mar 2026 reliability
  degradation, `#15419` frequent 503 bursts, `#15910` mid-call
  connection resets, `#13770` client-side ConnectTimeout) — Ollama
  has not published a status page, so the app must absorb upstream
  chaos by design. The previous catalog path used `connect=2.0s` with
  no retry; a single TCP RTT spike flipped the user-visible alert red
  until the next 30s poll. Layered logging at every transition —
  `upstream_http.retry_attempt`, `upstream_http.retry_after_honored`,
  `upstream_http.circuit_opened/half_open_probe/closed` — gives
  operators grep-friendly triage. Tests:
  `web/backend/tests/test_upstream_http.py` (9).

- **Hysteresis on `last_probe_status()` (`app.services.ollama_models`).**
  A rolling-3 deque of attempt outcomes per `base_url`; status flips
  to `"down"` only when 2-of-3 recent attempts failed. THIS is the
  load-bearing user-visible change of v0.2.5+hf.4: a single 2-second
  TCP spike used to flash the red alert until the next poll cycle.
  After hysteresis, single transients are absorbed silently while two
  failures in a row legitimately reports `"down"`. Companion
  `recent_attempts()` exposes the deque as a list-of-dicts for the
  health endpoint so the UI can render a "last 3 polls" indicator.
  Tests: `web/backend/tests/test_ollama_models_resilience.py` (8).

- **Stale-while-revalidate in `list_ollama_models()`.** When the cache
  is populated but expired (>5min), the function returns the stale
  list IMMEDIATELY and spawns a background refresh task. The
  user-facing catalog endpoint stays snappy (no cold-fetch latency
  every 5 minutes); the cache renews concurrently. `_in_flight_refresh`
  deduplicates concurrent stale-serve requests so we never schedule
  parallel refreshes for the same base_url.

- **Lifespan `upstream_warmup` hook
  (`web/backend/app/lifespan_hooks/upstream_warmup.py`).** On startup:
  spawns a fire-and-forget `list_ollama_models()` call (20-second
  internal timeout) so the singleton client's DNS + TLS handshake to
  Ollama Cloud is paid before the first user-facing request hits it.
  Also spawns a background refresh loop running `list_ollama_models()`
  every 240 seconds (just before the 5-min cache TTL boundary) so the
  cache never goes cold during steady-state operation. On shutdown:
  cancels the refresh task, awaits it, then `upstream_http.close_client()`
  to release the pool. Warmup MUST NOT block startup — pathological
  hangs in `list_ollama_models` are bounded by `asyncio.wait_for` and
  the app starts anyway. Tests:
  `web/backend/tests/test_lifespan_upstream_warmup.py` (3).

- **Per-run wall-clock safety net
  (`TRADINGAGENTS_RUN_MAX_SECONDS`).** `app.services.run_service._run_async`
  now wraps the engine call in `asyncio.wait_for(_run_engine(...),
  timeout=run_max)` with a default of 1800 seconds (30 minutes). On
  `asyncio.TimeoutError` the cooperative `cancel_event` is set so the
  engine's worker thread stops cleanly at the next chunk boundary,
  the run is marked `failed` with a clear "exceeded
  TRADINGAGENTS_RUN_MAX_SECONDS" error, and `GLOBAL_RUN_LOCK` is
  released by the existing `finally:` cleanup so the next run can
  proceed. This is the outer safety net for the deadlock surface the
  Layer-4 heartbeat could observe but not kill — without it, a hung
  LLM call holds the single-concurrent-run lock indefinitely
  (the 2026-05-22 56-minute hang in commit `2ccfeda` could have
  blocked every subsequent run if the user hadn't cancelled
  manually). Tests:
  `web/backend/tests/test_run_service_timeout.py` (2).

### Changed

- **`/api/health` Ollama block enriched with `recent_attempts` +
  `circuit_state`.** Backwards-compatible additive fields on the
  existing `ollama: {...}` subblock. `recent_attempts` is a list of
  the last-3 probe outcomes (`{at, ok, error}`) so the UI can render
  a "last 3 polls" indicator; `circuit_state` is one of `"closed" |
  "open" | "half_open"` so the frontend can render a yellow
  "recovering" pill during half-open or a "cooling down" notice
  during open instead of the binary red-or-nothing alert. Schemas:
  `app.schemas.OllamaHealth` + `app.schemas.OllamaAttempt` (new).
  Frontend mirror: `web/frontend/src/lib/types.ts`.

- **`OllamaUpstreamAlert.tsx` renders three calibrated states.**
  `closed` + `status: "ok"` → no alert; `closed` + `status: "down"`
  → existing red sustained-outage alert (now only fires after
  hysteresis confirms 2-of-3 failures, not on a single transient);
  `half_open` → yellow "recovering — upstream cooling down" pill
  (`role="status"`, amber palette); `open` → red "upstream in
  cool-down" with explanation of the breaker behavior. The
  validation-error branch is unchanged. Tests:
  `web/frontend/src/__tests__/OllamaUpstreamAlert.circuit.test.tsx` (3).

### Fixed

- **The catalog/health probe no longer flaps on a single transient.**
  Pre-`hf.4` symptom: `/api/health` polled every 30s; one
  `ConnectTimeout('')` (2-second TCP timeout against `ollama.com/v1`)
  flipped `ollama.status` to `"down"`, surfacing the red
  `OllamaUpstreamAlert` on NewRun. Combined with the wider connect
  timeout (10s vs 2s), retry-on-transient, and 2-of-3 hysteresis, a
  single transient is now absorbed silently. The four-layer defense
  added in `2ccfeda` (v0.2.5+hf.3) hardened the *run* pipeline; this
  pass hardens the *health/catalog* pipeline to the same standard.

- **`GLOBAL_RUN_LOCK` no longer holds indefinitely on a stuck run.**
  See "Per-run wall-clock safety net" above. The 30-minute timeout
  ensures the lock is always released within a bounded window even
  when every other resilience layer fails.


- **Pre-flight liveness probe on `POST /api/runs` and `/retry`.** A
  real CRM run on 2026-05-22
  (`5ee02744-f384-4db4-8332-f84a3a8e3990`) hung for 56 minutes 34
  seconds because the engine was committed to a `kimi-k2-thinking`
  quick-think model that Ollama Cloud could not respond to — the
  one-click Retry above turns "fix the form" into "fix the long wait",
  but nothing was checking model liveness *before* the engine launched.
  The new `app.services.ollama_models.probe_model_liveness(model_id)`
  issues `POST {OLLAMA_BASE_URL}/chat/completions` with a no-op `ping`
  tool function and `tool_choice="auto"` (deliberately exercising the
  failure surface from `ollama/ollama#14542` — 500s emerge only when
  `tools=[…]` is present), 15-second read timeout, and reasoning-aware
  `max_completion_tokens` (200 for `requires_reasoning_split` or
  `"thinking"`-in-model-id; 1 otherwise — thinking models need budget
  to think before emitting a token). Result is one of
  `ok` / `timeout` / `http_5xx` / `http_4xx` /
  `degraded_empty_response` (the last when HTTP 200 returns empty
  content with no tool_calls and a non-`stop` finish_reason — a real
  failure mode for OpenAI-SDK consumers). 5xx responses are scanned for
  Ollama's `(ref: <uuid>)` upstream correlation ID. Cache TTL is 60s
  for healthy entries, 30s for unhealthy entries — long enough to
  dedup two POSTs in quick succession, short enough that recovery is
  reflected promptly. The router's existing
  `_validate_models_against_catalog` was refactored to
  `_validate_and_probe` and is called from BOTH `POST /api/runs` and
  `POST /api/runs/{id}/retry` so retrying a failed run cannot re-stage
  the original hang. On probe failure the 400 returns a structured
  `RunValidationError` (`code: upstream_model_unhealthy`,
  `unhealthy_models[]`, `suggested_alternatives[]`); the alternatives
  list is the intersection of `CURATED_2026_05`, the cached
  `/v1/models` listing, and the cached-healthy set (sorted
  alphabetically with `glm-5` pinned first when present, max 3). The
  frontend `OllamaUpstreamAlert` was extended with a `validation` prop
  that renders unhealthy models + ref IDs + clickable alternative
  badges; `NewRun.tsx` captures the 400 detail on mutation `onError`.
  Tests: `web/backend/tests/test_runs_preflight_probe.py` (10),
  `web/frontend/src/__tests__/OllamaUpstreamAlert.validation.test.tsx`
  (6).

- **In-run heartbeat for slow LLM calls (`llm_call_pending` SSE
  event).** Defense-in-depth on top of the pre-flight probe above. The
  56-minute hang above completed *7 successful LLM calls* before the
  8th stalled — proof that a probe-time healthy model can degrade
  mid-run, and that even a perfect pre-flight check cannot prevent the
  long-silent-then-fail UX on its own. The new wrapper around
  `NormalizedChatOpenAI.ainvoke` / `.invoke` emits an
  `llm_call_pending` event every 30 seconds while a call is pending,
  with `model`, `agent` (e.g. *"Fundamentals Analyst"*),
  `elapsed_seconds`, and `soft_warning: true` once elapsed ≥ 90s.
  `WebRunObserver.emit_progress` already had the
  thread-safe-from-worker plumbing (it uses
  `run_coroutine_threadsafe` when called off the main loop), so the
  heartbeat reuses that path. `agent_hint` is plumbed through
  `GraphSetup` and `trading_graph.py` so each LangGraph node tags its
  LLM calls with the role currently running. The frontend `RunView.tsx`
  renders the latest heartbeat as an inline status row beneath the
  active agent (*"Fundamentals Analyst waiting on kimi-k2-thinking…
  90s — provider may be unhealthy"*); the row is replaced when the
  next non-heartbeat event arrives. Users can now react to a stall
  within a minute rather than waiting 30+ minutes for the retry
  envelope to exhaust before seeing any error. The existing
  `GLOBAL_RUN_LOCK` + cancel path is unchanged — heartbeats are
  observational, not interventional. Tests:
  `tests/test_llm_call_heartbeat.py` (Python, fake-clock based to
  avoid sleeping in CI),
  `web/frontend/src/__tests__/RunView.heartbeat.test.tsx` (3).

- **One-click Retry button on failed and cancelled runs.** Until now,
  every transient upstream LLM failure forced the user back to
  `NewRun.tsx` to re-fill ticker, date, provider, models, analysts,
  depth, language, checkpoint, and thinking-config from scratch. The
  new `POST /api/runs/{id}/retry` endpoint reconstructs a `RunRequest`
  from the parent row's persisted columns and delegates to the same
  submit path as `POST /api/runs` (including catalog re-validation),
  returning `{run_id, parent_run_id}` — same response shape as
  `/resume`. Two button placements in
  `web/frontend/src/routes/RunView.tsx` for two reading paths: primary
  in the header alongside Cancel/Resume, secondary inside the error
  banner so the affordance sits visually adjacent to the failure
  reason. Surfaces only when status is `failed` or `cancelled`
  (`completed` / `running` / `queued` aren't retry-shaped;
  `interrupted` already has `/resume`). Triggered by run
  `95629918-f57a-4bc3-a95f-16d5f66318e2`, where an Ollama Cloud
  `ref: fd44ca4b-...` HTTP 500 dead-ended a CRM analysis and the user
  had to re-fill the entire form. Tests:
  `web/backend/tests/test_runs_retry.py` (7),
  `web/frontend/src/__tests__/RunView.retry.test.tsx` (4).

- **`ADMIN_PASSWORD_HASH_B64` env-var fallback.** Coolify (and likely
  other PaaS env-var stores) silently interpolate `$<name>` references
  inside values *regardless* of their "literal" / "multiline" flags.
  bcrypt hashes always contain three `$` characters
  (`$2b$<cost>$<salt+digest>`), so each `$<chars>` segment is dropped to
  empty and the container receives a 45–46-char truncated hash that
  bcrypt cannot verify against any password. The new
  `admin_password_hash_b64` setting in `web/backend/app/config.py`
  accepts the base64 of the bcrypt hash (base64 has no `$` chars to
  interpolate) and a `@model_validator(mode="after")` decodes it into
  the canonical `admin_password_hash` at startup. The deploy still
  fails loudly via `min_length=60` if neither form is set or both
  decode to <60 chars. New tests:
  `test_settings_accepts_admin_password_hash_b64`,
  `test_settings_rejects_malformed_admin_password_hash_b64`.

### Changed

- **Per-model `read_timeout_seconds` for reasoning/thinking models.**
  The 120-second global read-timeout added in the previous hardening
  pass is the right floor for fast models but a hostile ceiling for
  reasoning models — `kimi-k2-thinking`, `gpt-oss:120b` with
  `reasoning_effort != "none"`, and `deepseek-v3.2` in reasoning mode
  can legitimately need 2–4 minutes per call when responding
  correctly, and the previous 120s cap turned every healthy slow call
  into a timeout that grew (via 5 SDK retries + LangGraph node retries)
  into a 30+ minute envelope. The `ModelCapabilities` dataclass in
  `tradingagents/llm_clients/capabilities.py` gained an optional
  `read_timeout_seconds: int | None` field, populated for the known
  reasoning models (`kimi-k2-thinking` / `kimi-k2.5` / `kimi-k2.6` →
  300; `gpt-oss:120b` / `gpt-oss:20b` → 300; `deepseek-v3.2` → 240),
  with `_BY_PATTERN` regex coverage for forward-compat
  (`^kimi-k2.*-thinking$`, `^.+:thinking$`). Precedence in
  `openai_client._construct_timeout` is **env `TRADINGAGENTS_LLM_READ_TIMEOUT`
  > capability override > 120s default** — operators who deliberately
  set a deploy-wide tight latency budget still win. The frozen
  dataclass is back-compat: existing `ModelCapabilities(...)`
  constructor calls without `read_timeout_seconds` still work. Tests:
  `tests/test_per_model_timeout.py` (7).

- **Curated Ollama Cloud catalog flag + UI deprioritization signal.**
  Two related concerns surfaced once the live model discovery from the
  previous Ollama-Cloud fix started returning ~39 models from
  `/v1/models` — Ollama Cloud's curated lineup (visible at
  `https://ollama.com/search?c=cloud`) is roughly half of what
  `/v1/models` advertises, and models *not* in the curated view tend
  to be older or de-prioritized SKUs with known reliability issues
  (`kimi-k2-thinking` is the smoking gun, but `qwen3-coder:480b`,
  `gemma3:4b` and several others match the pattern; see
  `ollama/ollama#15453` for the 95% failure rate snapshot). New
  `app.services.ollama_curated.CURATED_2026_05` frozenset captures
  the active cloud catalog as of 2026-05-23 (refresh quarterly).
  `is_curated(model_id) -> bool` is reused by both the catalog flag
  and the pre-flight probe's `suggested_alternatives` algorithm.
  `/api/catalog/models?provider=ollama` now emits `curated: bool` on
  each model (`CatalogModel.curated: Optional[bool]`), and
  `response_model_exclude_none=True` on the endpoint keeps non-Ollama
  responses from leaking the field. Frontend `NewRun.tsx` uses a new
  `<ModelOptionLabel>` + `sortCuratedFirst()` extracted into
  `web/frontend/src/components/ModelOptionLabel.tsx` (the
  jsdom-hang-friendly extraction pattern that `OllamaUpstreamAlert`
  established earlier) — curated models sort first; non-curated render
  with a `⚠` prefix and a tooltip naming three safer alternatives
  (*"Not in Ollama's active cloud catalog. May have reliability
  issues — consider glm-5, kimi-k2.6, or glm-5.1."*). `undefined`
  curated is treated as curated so older backends and non-Ollama
  providers don't get retroactive warning badges. Explicitly: we do
  NOT hide deprioritized models — they're still legitimately
  selectable, just risky. Tests:
  `web/backend/tests/test_catalog_curated_flag.py` (5),
  `web/frontend/src/__tests__/NewRun.curated.test.tsx` (8).

- **Per-provider `max_retries` defaults for OpenAI-compatible LLM
  clients.** The vendored OpenAI SDK defaults to `max_retries=2` with
  sub-second backoff — 3 total attempts inside ~2 seconds, useless
  against any sustained provider transient. (The Ollama Cloud HTTP 500
  that triggered this hardening pass exhausted all three attempts in
  1.75 seconds.) Cloud OpenAI-compatible providers (xai, deepseek,
  qwen*, glm*, minimax*, ollama, openrouter) now default to `5`
  retries — a ~32-second envelope with exponential backoff and jitter,
  matching the order-of-magnitude transient-LLM recovery window seen
  in practice. Native `openai` stays at `2` because OpenAI's own infra
  is reliable and extra retries amplify rate-limit penalties.
  `TRADINGAGENTS_LLM_MAX_RETRIES` (integer) overrides per-provider
  defaults at the env layer; explicit `max_retries` kwarg still wins.
  Precedence: kwarg > env var > per-provider default.

- **Explicit HTTP timeout on chat completions.** Chat-completions
  previously inherited httpx's 10-minute default, which let a hung
  upstream pin the `GLOBAL_RUN_LOCK` (single-concurrent-run lock) for
  the full window before the run dies. The new default is
  `httpx.Timeout(connect=10s, read=120s, write=10s, pool=10s)`. The
  120-second read is generous enough for thinking-model first-token
  latency but bounded so a hung upstream releases the run lock instead
  of pinning the app for minutes. `TRADINGAGENTS_LLM_READ_TIMEOUT`
  (seconds) replaces only the read field; explicit `timeout` kwarg
  replaces the whole `Timeout` object.

- **Node-level `RetryPolicy` on every LangGraph node.** A second
  resilience layer on top of the SDK retries above. LangGraph v1
  attaches retry policies per node (the `retry_policy` field on
  `StateNodeSpec`), not on `compile()` —
  `tradingagents/graph/trading_graph.py` mutates `workflow.nodes` to
  stamp a shared `_TRANSIENT_RETRY_POLICY` (max_attempts=3,
  initial_interval=8s, backoff_factor=2.0, jitter=True) on every node
  at all three compile sites (no-checkpointer, with-checkpointer,
  cleanup recompile). Catches `openai.InternalServerError`,
  `openai.APITimeoutError`, `openai.APIConnectionError`, and
  `httpx.RemoteProtocolError`. Why both layers: SDK retries handle
  transient *request* failures fast (≤32 s envelope); graph retries
  handle transient *node-execution* failures — including those that
  bubble past the SDK as raised exceptions — with the longer 8–16 s
  spacing that catches recoveries the tighter SDK window misses. Two
  layers cover different transient profiles without over-doubling
  cost (graph retries reuse the same node input; SDK retries reuse
  the same HTTP payload).

- **Engine errors are classified into operator-actionable strings
  before being persisted to `runs.error_message`.** The previous
  catch-site format `f"{type(exc).__name__}: {exc}"` produced a
  stack-trace-shaped string (`InternalServerError: Error code: 500 -
  {'error': 'Internal Server Error (ref: fd44ca4b-...)'}`) that gave
  the user no usable next step. The new
  `_format_engine_error(exc, provider)` helper in
  `web/backend/app/services/run_service.py` recognises the OpenAI SDK
  exception hierarchy and renders complete sentences naming the
  provider, surfacing the upstream `ref:` correlation ID, and
  suggesting next actions: `InternalServerError` / generic 5xx →
  *"Upstream provider error (ollama, HTTP 500). This is usually
  transient. Reference: fd44ca4b-... Click Retry below, or pick a
  different model if it persists."*; `APITimeoutError` → *"timed out
  … try a smaller/faster model"*; `APIConnectionError` /
  `httpx.ConnectError` → *"Could not reach … verify network and
  base URL"*; `AuthenticationError` → *"Authentication failed …
  verify the API key environment variable"*; `RateLimitError` →
  *"Rate limited … wait and Retry"*; `BadRequestError` → preserves
  full upstream detail (it's usually a config bug needing the detail
  to fix). Anything unrecognised falls back to the legacy
  `repr`-style so unknown failure modes still leave a usable trace.
  Classification ordering matters because `APITimeoutError` inherits
  from `APIConnectionError`, and `AuthenticationError` /
  `RateLimitError` / `BadRequestError` all inherit from
  `APIStatusError` — the 5xx branch explicitly excludes them so a
  quirky provider returning `status_code=500` on an auth error
  doesn't get mis-classified. No schema change: the formatted string
  goes into the existing `error_message` `Text` column.

- **`bcrypt<4.0` pin.** passlib 1.7.4 (still its last released version)
  reads `bcrypt.__about__.__version__` to choose its backend. That
  attribute was removed in bcrypt 4.0, so every `bcrypt.verify()`
  emits `(trapped) error reading bcrypt version` and on some hosts the
  fallback path silently fails verification. Pinning to the last 3.x
  release restores passlib's fast path and removes the warning. Drop
  the pin when passlib ships a release that drops the version check
  (track [passlib#190](https://foss.heptapod.net/python-libs/passlib/-/issues/190)).

- **Dropped the bespoke `_is_https()` / `is_secure_request()` helpers.**
  Replaced with `request.url.scheme == "https"`, which Starlette's
  `ProxyHeadersMiddleware` (enabled via uvicorn `--proxy-headers`) sets
  correctly behind a trusted reverse proxy. The previous helpers parsed
  the raw `X-Forwarded-Proto` header directly, which would have been
  attacker-controllable in any deployment topology without a strict
  proxy whitelist.

### Fixed

- **Ollama provider/model dropdowns now reflect what the configured
  endpoint can actually serve.** Two related bugs in one fix. (1) The
  hardcoded Ollama entries in `tradingagents/llm_clients/model_catalog.py`
  listed *local-Ollama* tags (`qwen3:latest`, `glm-4.7-flash:latest`,
  `gpt-oss:latest`) — but the deployed app points `OLLAMA_BASE_URL` at
  Ollama Cloud (`https://ollama.com/v1`), whose model IDs are different
  (`gpt-oss:120b`, `qwen3-coder:480b`, `glm-4.7`, …). The UI offered the
  user three model names that physically could not exist on the
  configured endpoint, producing a 10-second-late `NotFoundError: model
  "qwen3:latest" not found` from the engine's first chat-completions
  call. The catalog now lives-discovers Ollama models at request time
  via `GET {OLLAMA_BASE_URL}/models` (the OpenAI-compatible list
  endpoint that both local and Cloud Ollama implement), with a 5-minute
  TTL cache that returns the last-good list on upstream failure so the
  UI never goes blank. (2) `GET /api/catalog/providers` used to return
  every provider the codebase knew about, regardless of whether the
  deployment had credentials for any of them. The deployed app today
  has only an Ollama Cloud token, so picking any other provider 401'd /
  404'd ~10s into the run. Providers are now filtered by env-credential
  presence (`tradingagents.providers.available_providers`), so the
  dropdown is honest about what's actually wired up. Defense in depth:
  `PUT /api/settings/defaults` and `POST /api/runs` both validate the
  picked model against the live catalog and 400 before any state is
  persisted; `GET /api/settings/defaults` auto-heals stale saved
  values (the previously-saved `qwen3:latest` returns as `null` so the
  form re-prompts). `GET /api/health` gains an optional `ollama` block
  reporting upstream reachability when Ollama is the active provider.
  The probe distinguishes `"ok"` (last fetch succeeded — `model_count`
  may legitimately be `0`), `"down"` (last fetch failed — `error`
  carries the reason), and `"unknown"` (no probe attempted in this
  process) so an account with zero provisioned models doesn't trip a
  false "down" alert. The `New Analysis` form now shows an inline
  warning (`useHealth()` polls /api/health every 30s) when the
  selected provider is Ollama and the upstream probe says down, so the
  user gets the failure signal before submit instead of 10 seconds
  after. `GET /api/health` is now typed end-to-end via a
  `HealthResponse` Pydantic model mirrored in
  `web/frontend/src/lib/types.ts`.
- **Alembic migration `0002` adds the missing `login_attempts.id` column.**
  The 0001 migration omitted any primary key from `login_attempts` (the
  table is queried by `(ip, attempted_at)`, so a PK isn't needed for the
  hot path), but the ORM in `app/models.py` declares `id` as the
  primary key — meaning SQLAlchemy emits `INSERT ... RETURNING id` on
  every rate-limit record. On Postgres this 500'd every login attempt
  (`UndefinedColumnError: column login_attempts.id does not exist`).
  Test suites missed it because `tests/conftest.py` uses
  `Base.metadata.create_all` (which generates schema from the ORM, not
  from migrations), so the production-only migration-vs-ORM drift was
  invisible. The 0002 migration drops and recreates the table — losing
  a few rate-limit rows is safe because the limiter's in-memory state
  is also persisted to the same table.
- **Dockerfile `COPY --from=fe` now sources from where vite actually
  writes.** `web/frontend/vite.config.ts:25` sets `outDir` to
  `path.resolve(__dirname, "../backend/app/static")` — convenient for
  local dev (the build lands directly in FastAPI's static dir without a
  copy step) but inside the Docker `fe` stage that path resolves to
  `/backend/app/static`, not the conventional `/fe/dist`. The Dockerfile
  now copies from the correct location and adds a `test -s
  /backend/app/static/index.html` assertion so a future regression
  (e.g. a CACHED-but-empty BuildKit layer) fails loudly at build time
  instead of producing an image with no SPA assets.
- **`web/frontend/src/lib/assetType.ts` is now tracked.** The repo-root
  `.gitignore` had a bare `lib/` pattern (from the stock Python
  template) that recursively matched `web/frontend/src/lib/`. The
  earlier sibling files (`api.ts`, `sse.ts`, etc.) were already tracked
  before that rule landed; `assetType.ts` added later was silently
  ignored, breaking the frontend build on every fresh clone. The
  Python-packaging patterns in `.gitignore` (`build/`, `dist/`, `lib/`,
  etc.) are now scoped to repo root with leading slashes.

### Removed

- **The three `CLAUDE.md` files dropped from public tracking.** Root,
  `web/backend/`, and `web/frontend/` `CLAUDE.md` are now git-ignored
  — they're local-only AI-session conventions and their dense
  internal-conventions content was not worth exposing as part of the
  public fork's surface area. Earlier versions remain in git history
  (cannot be retroactively scrubbed without a destructive force-push
  that would break existing clones). The `.gitignore` also gained
  pre-emptive patterns for `THREAT-MODEL*.md`, `SECURITY-REVIEW*.md`,
  `*.security.md`, and `.security/` so future security-review output
  never reaches the public repo accidentally.

### Security

- **Three production secrets now hard-required at startup.** `JWT_SECRET`,
  `FERNET_KEY`, and `ADMIN_PASSWORD_HASH` no longer have defaults in
  `web/backend/app/config.py`. Pydantic refuses to construct `Settings`
  if any is unset or shorter than its enforced `min_length` (32 / 44 /
  60). Closes the misconfigured-deploy failure mode where the previous
  `default="dev-jwt-secret-change-me"` left a publicly-known signing key
  in effect whenever an operator forgot to set the env var — anyone
  reading the public fork could have forged a valid admin JWT.
- **Login rate-limit no longer bypassable via spoofed `X-Forwarded-For`.**
  `services/rate_limit.py:client_ip` previously read the leftmost XFF
  entry, which is directly attacker-controllable even behind a trusted
  proxy. Now reads `request.client.host`, populated from the trusted
  proxy hop by uvicorn's `--proxy-headers --forwarded-allow-ips='*'`
  flag added to `entrypoint.sh`. Without this fix, an attacker could
  rotate spoofed XFF on every `/api/auth/login` call and brute-force the
  admin password at unrestricted rate.
- **Content-Security-Policy `script-src` no longer permits
  `'unsafe-inline'`.** The Vite production build emits only external
  module scripts, so the directive was unused but dangerous: a future
  commit adding `rehype-raw` to the report renderer would have turned
  any LLM-injected `<img onerror=>` into stored XSS without CSP
  catching it. `style-src` keeps `'unsafe-inline'` because Tailwind
  needs it for utility-class injection.
- **Container runs as non-root `tradingagents` (UID 10001).** Dockerfile
  adds the user, chowns `/data/tradingagents` and `/app`, and switches
  via `USER tradingagents` before the entrypoint. Limits the blast
  radius of any code-execution attack against the running process —
  previously a successful exploit landed as root with full write access
  to the mounted volume.

### Operations

- **`DEPLOY.md` documents the Coolify `$`-mangling trap end-to-end.**
  New "Coolify-specific gotcha" callout in Step 1, the env-var table
  now lists `ADMIN_PASSWORD_HASH` and `ADMIN_PASSWORD_HASH_B64` as
  alternatives (with footnote ¹), and Troubleshooting adds a "Login
  returns 401 on the correct password" section with a copy-paste
  diagnostic that exec's into the container to dump
  `os.environ['ADMIN_PASSWORD_HASH']` and run a live bcrypt verify.

## [0.2.5+hf.1] — 2026-05-21

First cut of the fork as a versioned artifact. Captures every fork-only
commit landed on `main` between upstream tag `v0.2.5` and this release —
notably the Web UI surface, the Coolify-targeted deploy pipeline, and a
handful of upstream-PR backports that hadn't yet shipped in upstream
`main`.

**Baseline:** built on [`TauricResearch/TradingAgents` v0.2.5](https://github.com/TauricResearch/TradingAgents/releases/tag/v0.2.5).

### Added

- **Web UI — single-tenant FastAPI backend + React frontend.** A
  login-gated browser interface to the trading-agents pipeline: kick off
  runs, watch agent status and reports stream in over SSE, browse run
  history, manage per-provider API keys. Backend uses an auto-discovery
  registry pattern for routers / middleware / lifespan hooks so adding a
  new endpoint is "drop a file, no wiring." Frontend is Vite + React 18
  + TypeScript 5 + Tailwind 3 + shadcn primitives, with a typed
  `RunEvent` discriminated union shared between the SSE stream and the
  client reducer. Ships with an e2e Playwright suite and a pytest backend
  suite that runs against an in-memory SQLite engine.
- **Multi-stage Docker build + Coolify deploy guide.** One image bundles
  the backend, the built frontend assets, and the `tradingagents` engine
  itself; `entrypoint.sh` runs Alembic migrations on boot and then
  `uvicorn --proxy-headers`. A `DEPLOY.md` walks through the Coolify
  side: required env vars (`JWT_SECRET`, `FERNET_KEY`,
  `ADMIN_PASSWORD_HASH`), volume mounts for `~/.tradingagents`, and the
  health-check contract.
- **`OLLAMA_API_KEY` env var for Ollama Cloud auth.** The OpenAI-
  compatible Ollama client now picks up an API key when present, so the
  same code path works against a self-hosted `ollama serve` (no auth) and
  the managed Ollama Cloud endpoint (bearer token required).
- **Analysis-only crypto asset mode.** A new `tradingagents/asset_types.py`
  module centralises crypto-vs-equity detection (suffix-based:
  `-USD`, `-USDT`, etc.) and the analyst-set filtering rules — crypto
  runs skip fundamentals and route market/news/sentiment through the
  crypto-appropriate data path. Integrates the design from upstream
  PR #567 plus the analyst-execution-plan scaffolding from PR #487.
- **`tradingagents/run_observer.py` — shared streaming module.** Extracts
  `RunObserver` + `stream_run` out of `cli/main.py` so both the CLI and
  the web backend can drive a graph run identically, emitting the same
  event taxonomy. The CLI now consumes this module rather than owning
  its own observer.
- **`tradingagents/providers.py` and shared catalog/env-var modules.**
  `PROVIDERS`, `MODEL_OPTIONS`, and `PROVIDER_API_KEY_ENV` are now
  importable from a single place; the CLI dropdown and the web settings
  page consume the same source of truth, eliminating the previous
  duplicate provider lists.
- **`run_crm.py` programmatic one-off driver.** A small Windows-safe
  entry point (reconfigures stdout/stderr to UTF-8 before the trading-
  agents imports load) for batch / cron-style invocations that don't
  want the interactive CLI.
- **Root `CLAUDE.md` + nested `web/backend/CLAUDE.md` + `web/frontend/CLAUDE.md`.**
  Authoritative guidance for AI coding sessions: registry patterns,
  security invariants, async-loop conventions, and the load-bearing-files
  list per subtree.
- **Comprehensive Web UI developer docs** under `web/docs/` — architecture,
  API reference + SSE event taxonomy, backend-dev guide,
  frontend-dev guide, testing conventions, and operations runbook.

### Changed

- **Shared modules extracted from `cli/`.** Provider and asset-type logic
  moved out of `cli/utils.py` so the web backend can reuse them without
  pulling in CLI-only dependencies. CLI behaviour is unchanged; the
  imports just resolve to the shared modules now.
- **`cli/main.py` slimmed down.** Run streaming + observer logic moved to
  `tradingagents/run_observer.py`; `cli/main.py` is now a thin Typer
  layer that wires the observer to Rich console output. Same UX, fewer
  responsibilities per file.

### Fixed

- **Anthropic `effort=` kwarg skipped on non-supporting models** (backport
  of upstream PR #831). Older Claude models reject the `effort` parameter
  the model catalog now sends to newer ones; the binding flow consults
  the capability table and omits the kwarg where the model doesn't
  declare support.
- **MiniMax `reasoning_split` gated by model capability** (backport of
  upstream PR #826). Only M2.x models that emit the
  `reasoning_content` channel get the split treatment; older MiniMax
  models that pack reasoning into the main content stream are left
  alone.
- **Sentiment analyst label + asset-type propagation through the graph.**
  Integrates upstream PRs #487 + #567 cleanly: sentiment label stays
  "Sentiment Analyst" across the CLI / status panel / saved reports,
  the asset-type flag is threaded through `propagation.py` so analyst
  filtering activates, and the analyst execution plan honours the
  filtered set.
- **Web backend routers auto-discover before `tradingagents` is
  installed.** The Coolify build originally failed when the backend
  package was imported before `pip install -e .` of the engine; the
  Dockerfile ordering is fixed and the auto-discovery loader tolerates
  partial imports during boot.

[Unreleased]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5-hf.1...HEAD
[0.2.5+hf.1]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5...v0.2.5-hf.1
