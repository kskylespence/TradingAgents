# Operations

> Audience: operators and on-call for a deployed TradingAgents Web UI.

This is the runbook for what to do once the app is running. The full
Coolify deployment walkthrough lives in [`../../DEPLOY.md`](../../DEPLOY.md);
do not re-read it here.

## Environment variable reference

Settings are loaded by `app/config.py` (pydantic-settings). Every var is
case-insensitive and may also be provided via a `.env` file at the
backend working directory. Anything not listed below is ignored
(`extra="ignore"`).

### Required

| Name | What it does |
|---|---|
| `ADMIN_USERNAME` | The single user's login name. |
| `ADMIN_PASSWORD_HASH` | bcrypt hash of the admin password (`passlib.hash.bcrypt`). The plaintext password is never stored. Required (no default); enforced `min_length=60` because every bcrypt hash is exactly 60 chars. Generate with the recipe in [`DEPLOY.md` Step 1](../../DEPLOY.md). |
| `JWT_SECRET` | HMAC secret for signing the `access_token` cookie (HS256, `app/auth.py`). Required (no default); enforced `min_length=32`. Rotating it invalidates every existing session — see [Secret rotation](#secret-rotation). 64 hex chars from `openssl rand -hex 32`. |
| `FERNET_KEY` | Master key used by `app/crypto.py` to encrypt provider API keys at rest in the `api_keys` table. Required (no default); enforced `min_length=44`. Produce with `Fernet.generate_key()` (44 url-safe base64 chars). **Lose this key and the stored keys become unreadable forever.** |
| `DATABASE_URL` | SQLAlchemy async URL. Production: `postgresql+asyncpg://USER:PASS@HOST.neon.tech/DB?ssl=require`. Local dev defaults to `sqlite+aiosqlite:///:memory:`. |

The three secrets above (`ADMIN_PASSWORD_HASH`, `JWT_SECRET`, `FERNET_KEY`)
are **hard-required by pydantic-settings**: if any is unset or below its
`min_length`, the app raises `ValidationError` at startup naming the
offending field. There is no silent default fallback. This is deliberate
defense against the misconfigured-deploy failure mode where the app
silently runs with a publicly-known dev string.

### Optional

| Name | Default | What it does |
|---|---|---|
| `JWT_TTL_SECONDS` | `604800` (7 days) | How long an issued JWT is valid. Rotating this does not retroactively shorten existing tokens. |
| `DATA_DIR` | `/data/tradingagents` | Where reports, checkpoints, and the memory log live. Must match the Coolify volume mount path. |
| `RETENTION_DAYS` | `90` | How old report dirs and checkpoint files must be before the [disk pruner](#disk-pruner) deletes them. |
| `APP_ENV` | `development` | Free-form label surfaced in `backend.startup` logs. Set to `production` in prod. |
| `DEBUG` | `false` | When true, exposes the FastAPI OpenAPI UI at `/api/docs`. Leave off in prod. |
| `COOLIFY_FQDN` | unset | Auto-injected by Coolify. Used for CSP construction. |
| `COOLIFY_URL` | unset | Auto-injected by Coolify. Used for CSP construction. |
| `FAKE_LLM` | unset | When `=1`, `run_service._run_engine` short-circuits to a scripted simulator (~0.3 s, always returns Buy). Dev and test only — never set in prod. |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Where the catalog's live-discovery service points when listing Ollama models, and where the engine sends chat completions. Set to `https://ollama.com/v1` for Ollama Cloud. **Setting this env var is what makes Ollama appear in the provider dropdown** — `tradingagents.providers.available_providers()` treats unset = "not configured". |
| `OLLAMA_API_KEY` | unset | Bearer token forwarded as `Authorization: Bearer <key>` on every Ollama request. Required for Ollama Cloud; unset (or any value) is fine for a local `ollama serve` that doesn't auth. |
| `TRADINGAGENTS_LLM_PROVIDER` | unset | Provider key (e.g. `ollama`, `openai`). Read by `/api/health` to decide whether to include the `ollama` upstream-probe block in the response — set this when deploying so the health endpoint surfaces upstream reachability honestly. Used as a default by the engine path too. |
| `TRADINGAGENTS_LLM_MAX_RETRIES` | `5` for cloud providers, `2` for native `openai` | Overrides the per-provider default `max_retries` passed to the OpenAI-compatible chat client. The vendored SDK's bare default (`2` with sub-second backoff) burns through 3 attempts in under 2 seconds, which lost a real-world run when Ollama Cloud was 500-ing — `5` gives a ~32-second envelope with exponential backoff and jitter. Bump this if your provider's transients run longer than 30 seconds; drop it if you want to fail fast. Explicit `max_retries` kwarg passed by callers still wins. |
| `TRADINGAGENTS_LLM_READ_TIMEOUT` | `120` (seconds) | Replaces the `read` field of the `httpx.Timeout` applied to chat completions. The full default is `Timeout(connect=10, read=120, write=10, pool=10)`. The 120-second read is generous for thinking-model first-token latency but bounded so a hung upstream releases the `GLOBAL_RUN_LOCK` instead of pinning the app for httpx's 10-minute default. `connect`/`write`/`pool` are not env-overridable; an explicit `timeout` kwarg passed by callers replaces the whole `Timeout` object. |
| `TRADINGAGENTS_RUN_MAX_SECONDS` | `1800` (30 min) | **v0.2.5+hf.4 — outer safety net.** Maximum wall-clock duration of a single run, enforced by `asyncio.wait_for` around `_run_engine` in `run_service._run_async`. On timeout, the cooperative `cancel_event` is set, the run is marked `failed` with a clear error naming this env var, and the existing `finally:` block releases `GLOBAL_RUN_LOCK` — so the single-concurrent-run lock cannot be pinned by a hung LLM call beyond this window. Bump for legitimately long thinking-model runs; the default covers all observed runs to date. |

## Lite VPS preset

For a **2 vCPU / 8 GB** host running only the web UI with a **cloud LLM**
(no local `ollama serve` on the same box), set these in Coolify before
your first analysis run:

```env
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
TRADINGAGENTS_MAX_RISK_ROUNDS=1
TRADINGAGENTS_RUN_MAX_SECONDS=1200
TRADINGAGENTS_LLM_READ_TIMEOUT=90
TRADINGAGENTS_LLM_MAX_RETRIES=3
```

Do **not** set `OLLAMA_BASE_URL` unless Ollama runs on a **remote**
host (or you use Ollama Cloud). Leaving it unset avoids background
catalog probes to `localhost:11434`.

In the **New Run** UI (per-run overrides):

- **Research depth**: Shallow (1)
- **Analysts**: Market + Sentiment only (skip News and Fundamentals)
- **Models**: mini / fast variants; avoid thinking models on small VPS

Fresh installs also get `research_depth=1` and `analysts=["market","social"]`
from `GET /api/settings/defaults` until the user saves different choices.

See [VPS troubleshooting](#vps-troubleshooting) if the host shut down during
deploy or a run.

## VPS troubleshooting

If the provider power-cycled the VM or SSH stopped responding, correlate
**when** it happened before redeploying:

```bash
# OOM kills — most common "mysterious shutdown" on small VPS hosts
sudo dmesg -T | grep -iE 'killed process|out of memory' | tail -20

# Memory snapshot while the app is running
docker stats --no-stream

# Was local Ollama competing for CPU/RAM?
systemctl status ollama 2>/dev/null
curl -s localhost:11434/api/tags 2>/dev/null | head

# Coolify build vs run: grep deploy window in app logs
docker logs <coolify-app-container> 2>&1 | tail -100
```

| Timing | Likely cause | Fix |
|--------|--------------|-----|
| During first Coolify deploy | Docker build (`npm ci`, Vite, `pip install`) | [Prebuilt GHCR image](../../DEPLOY.md#prebuilt-image-ghcr), add swap, or upgrade VPS |
| During first analysis run | Local Ollama on same host, or depth 5 + all analysts | Cloud LLM or remote Ollama; [lite preset](#lite-vps-preset) |
| Idle after deploy | Unlikely this app alone | Check Coolify/Traefik/other containers on the host |

### Per-provider API keys (optional)

Each of these can be pre-seeded at deploy time as an alternative to
entering them through **Settings → API Keys** in the UI. Any not set
here can be added later (the in-app value will encrypt to Postgres
under `FERNET_KEY`; the env-var value is read directly by the engine).

`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`,
`DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, `DASHSCOPE_CN_API_KEY`,
`ZHIPU_API_KEY`, `ZHIPU_CN_API_KEY`, `MINIMAX_API_KEY`,
`MINIMAX_CN_API_KEY`, `OPENROUTER_API_KEY`, `AZURE_OPENAI_API_KEY`,
`OLLAMA_API_KEY` (the Ollama Cloud bearer token; local Ollama needs no
key).

The canonical provider-to-env-var mapping is
`tradingagents.llm_clients.api_key_env.PROVIDER_API_KEY_ENV`.

## Secret rotation

| Secret | Effect of rotation | Effect of loss |
|---|---|---|
| `ADMIN_PASSWORD_HASH` | Next login uses the new hash; existing JWT sessions stay valid until their `exp` (default 7 days). | Re-generate from a new password. |
| `JWT_SECRET` | Every existing session is invalidated immediately — your "log out everywhere" lever. | Re-generate. Users re-log-in. |
| `FERNET_KEY` | Not currently supported (see below). | **Stored provider API keys become permanently unreadable.** No recovery. The user must re-enter them via the Settings page. Back this key up. |

### `JWT_SECRET` rotation

Generate a new value, update it in Coolify, restart the app. Every
cookie issued under the old secret now fails `decode_access_token` →
HTTP 401, and the browser redirects to `/login`. This is the intended
emergency response to a suspected token compromise.

### `FERNET_KEY` rotation (planned vs unplanned)

The `app/crypto.py` cache (`_get_fernet`) holds a single Fernet
instance built from the current `FERNET_KEY`. There is no fallback
key chain, so live key rotation is **not currently supported**. If you
must rotate:

1. Schedule a maintenance window — the app cannot serve `api_keys`
   reads or writes mid-migration.
2. Stop the container.
3. With the OLD key, decrypt every `api_keys.encrypted_value` row.
4. With the NEW key, re-encrypt each row and UPDATE.
5. Update `FERNET_KEY` in Coolify to the new value.
6. Start the container.

A `FERNET_KEY` LOSS (the old key is gone) has no recovery path. The
`api_keys` table cannot be decrypted. The remediation is: generate a
new key, deploy it, then re-enter each provider key through
**Settings → API Keys**. The history, run records, and reports are
unaffected — only the encrypted credential table is dead weight.

## Health states

`GET /api/health` is the Coolify liveness probe (`web/backend/app/routers/health.py`).

```json
{
  "status": "ok" | "degraded",
  "version": "0.2.5+hf.2",
  "db":     "ok" | "down",
  "disk_free_mb": 18234,
  "active_run_id": null,
  "ollama": {                             // present iff TRADINGAGENTS_LLM_PROVIDER=ollama
    "status": "ok" | "down" | "unknown",
    "url": "https://ollama.com/v1",
    "model_count": 39,                    // int on "ok"; null on "down"/"unknown"
    "error": null,                         // exception repr on "down"
    "recent_attempts": [                  // v0.2.5+hf.4 — rolling last-3 log
      {"at": "...", "ok": true,  "error": null},
      {"at": "...", "ok": false, "error": "ConnectTimeout('')"},
      {"at": "...", "ok": true,  "error": null}
    ],
    "circuit_state": "closed"             // v0.2.5+hf.4 — "closed" | "open" | "half_open"
  }
}
```

| `status` | `db` | HTTP code | What it means |
|---|---|---|---|
| `ok` | `ok` | 200 | DB reachable on a trivial `SELECT 1`. Normal operation. |
| `degraded` | `down` | 200 | DB unreachable. The app keeps serving static assets and login screen so a human can see the failure. |

The optional `ollama` block reports upstream LLM reachability when
Ollama is the active provider. `ollama.status` distinguishes:

- `"ok"` — last probe succeeded OR a single recent failure with two
  prior successes (**hysteresis added in 0.2.5+hf.4** — single
  transients no longer flip the alert red). `model_count` is the real
  count. Note that `model_count: 0` is still `"ok"` — an account
  legitimately provisioned with zero models is not "down".
- `"down"` — **2 of the last 3** probe attempts failed (sustained
  outage). `error` carries the latest exception repr for triage.
- `"unknown"` — no probe attempted yet in this process. Practically
  only seen at very cold start.

**Hysteresis (0.2.5+hf.4).** The previous binary "single failure →
down" logic flipped the user-visible alert red on every 2-second TCP
RTT spike against `ollama.com/v1`. The 2-of-3 rule absorbs single
transients silently while a real outage still surfaces within
two 30-second poll cycles. `recent_attempts` is the underlying log
the rule operates on.

**Circuit breaker (0.2.5+hf.4).** `circuit_state` mirrors the shared
`upstream_http` breaker:

- `"closed"` — normal operation.
- `"open"` — 5+ consecutive upstream failures; new requests
  short-circuit with `CircuitBreakerError` and the catalog falls back
  to last-good cache. Stays open for 30 s.
- `"half_open"` — cooldown elapsed; one trial probe in flight. A
  success closes the circuit; a failure reopens it.

Operators can grep for the breaker's transitions in the logs:
`upstream_http.circuit_opened`, `upstream_http.circuit_half_open_probe`,
`upstream_http.circuit_closed`. The intent is that a sustained Ollama
Cloud outage opens the breaker once (visible in logs + UI) instead of
amplifying the failure with hundreds of doomed retries.

**The outer `status` is NOT flipped to `"degraded"` when Ollama is
down.** Coolify treats non-2xx (and now `degraded`) as restart
signals; restarting won't fix the upstream. The body advertises the
failure for humans / dashboards while the container stays up. Same
invariant as `db: "down"`.

### Why `degraded` is still HTTP 200

Coolify treats any non-2xx health response as "container unhealthy"
and restarts the container. A transient Neon connection blip is not a
reason to restart — the app has nothing to fix by being killed. By
returning 200 with `status: "degraded"` we let humans and dashboards
see the failure while the container itself stays up. Module docstring
in `health.py` is the canonical justification.

If a probe ever needs strict 503 behaviour, layer a second path on top
(e.g. `/api/health/strict`). Today there is no such path.

## Crash recovery on startup

When the previous process was killed mid-run (Coolify redeploy, OOM,
manual `kill -9`), rows in `runs` are left at `status='running'` with
no live task. The `crash_recovery` lifespan hook
(`web/backend/app/lifespan_hooks/crash_recovery.py`) runs at every
boot:

1. Finds every `runs` row with `status='running'`.
2. Flips each row to `status='interrupted'`, sets `finished_at`, and
   appends a terminal `run_failed` event with `interrupted: true` so
   any reconnecting SSE client sees a clean stream end.
3. Marks the row `resumable=True` iff `checkpoint_enabled=true` AND a
   LangGraph SQLite checkpoint file exists at
   `<DATA_DIR>/cache/checkpoints/<TICKER_UPPER>.db` (see `has_checkpoint` in
   `services/crash_recovery.py`).

The hook is idempotent — a second call finds nothing because the first
already flipped every orphan.

**Log lines to look for**:

- `crash_recovery.no_orphans_found` — clean restart, nothing to do.
- `crash_recovery.transitioned_orphaned_runs` (with `count` + `run_ids`) — at
  least one orphan was recovered. The `extra.run_ids` array tells you
  exactly which runs.
- `crash_recovery.skipped_schema_not_ready` — DB schema not
  materialised. Expected on first-boot before Alembic has run, and in
  some test harnesses; do not page on this.
- `crash_recovery.failed` — anything else. Recovery is a soft
  failure (the next restart retries), but investigate.

From the user's perspective: their interrupted run appears in History
with status **Interrupted** and, when the checkpoint exists, a
**Resume** button. Clicking Resume creates a new run with the same
`(ticker, date)` so the LangGraph `thread_id` collides and the engine
picks up from the last checkpoint.

## Disk pruner

A background `asyncio.Task` started by
`web/backend/app/lifespan_hooks/disk_pruner.py` runs `prune_once`
every 6 hours (`services/disk_pruner.py:prune_loop`). Each pass:

- Deletes report directories under `<DATA_DIR>/logs/<TICKER>/<run>/`
  and `<DATA_DIR>/reports/<bundle>/` whose `mtime` is older than
  `RETENTION_DAYS` (default 90).
- Deletes checkpoint SQLite files under `<DATA_DIR>/cache/` named
  `<TICKER>-<DATE>-checkpoint.sqlite` when the matching `runs` row's
  `created_at` is older than `RETENTION_DAYS`. **Orphan checkpoint
  files** (no matching `runs` row) are kept — without a `created_at`
  signal we have no defensible cutoff and would punish operators who
  placed a checkpoint by hand.

### Path safety

Every `unlink`/`rmtree` target is validated by
`_safe_inside(candidate, data_dir)` which checks
`path.resolve().is_relative_to(data_dir.resolve())`. Anything that
resolves outside `DATA_DIR` is refused with a
`disk_pruner.refuse_outside_data_dir` warning. Nothing outside the
volume can be touched, even if a filename was crafted to escape.

### Log lines to look for

- `disk_pruner.task_started` (with `data_dir` + `retention_days`) — emitted once at boot.
- `disk_pruner.task_stopped` — emitted once at shutdown.
- `disk_pruner.pass_complete` (with `reports_deleted` + `checkpoints_deleted`) — once per 6-hour tick.
- `disk_pruner.report_deleted` / `disk_pruner.checkpoint_deleted` — per-file records.
- `disk_pruner.refuse_outside_data_dir` — should NEVER fire in normal operation. Investigate immediately if you see one.
- `disk_pruner.tick_failed` — a single tick raised; the next tick will run as scheduled.

## Log shape

The backend uses `stdlib logging.config.dictConfig` with the JSON
formatter in `app/logging_config.py`. Every line is one JSON object on
stdout, which Coolify captures and surfaces under the app **Logs** tab.

```json
{
  "ts":      "2026-05-19T14:23:10.182733+00:00",
  "level":   "INFO",
  "logger":  "app.services.run_service",
  "message": "run_service.engine_failed",
  "run_id":  "8c9f4e6c-...",
  "...extra": "..."
}
```

The `run_id` field is populated automatically from the
`run_id_var` ContextVar that `run_service` sets on the runner thread,
so every log emitted while a run is in flight is auto-tagged with its
UUID. Caller-supplied `extra={...}` fields are merged into the
payload; non-JSON-serialisable values fall back to `repr()`.

`uvicorn.access` and `sqlalchemy.engine` are quieted to WARNING to keep
the runner's own structured logs the dominant signal.

## Grep recipes — when X happens, look for Y

| Symptom | Where to look |
|---|---|
| SSE clients keep disconnecting mid-run | `grep event_bus.queue_full_dropped_frame` — a slow subscriber's per-run queue (size 200) overflowed and the live frame was dropped. The DB row is intact; the client recovers on reconnect via `Last-Event-ID`. If you see many of these for one `run_id`, the network path between the client and Coolify is the suspect. |
| Health endpoint returns `degraded` | `grep health.db_check_failed` for the underlying exception; check the Neon dashboard; verify `DATABASE_URL` is reachable from the container. |
| Login lockout, "I'm locked out" | The 5-failures-per-5-minutes ban is persisted to `login_attempts`. Inspect with `SELECT ip, COUNT(*) FROM login_attempts WHERE attempted_at > now() - interval '1 hour' AND succeeded = false GROUP BY ip;` from the Neon SQL editor. To clear: `DELETE FROM login_attempts WHERE ip = '<your-ip>'::inet;` (also documented in [`DEPLOY.md` Troubleshooting](../../DEPLOY.md)). |
| A run is stuck at status `running` | If the container is alive and there is no live task driving it, restart the container — the `crash_recovery` startup hook flips it to `interrupted` and emits the terminal SSE event. Look for `crash_recovery.transitioned_orphaned_runs` in the boot logs. |
| Disk filling up | `grep disk_pruner` for recent pass-complete counts. If `reports_deleted` is 0 over many passes, your `RETENTION_DAYS` may be set higher than needed. |
| Provider call returned 401 / decrypt failed | `grep run_service.api_key_decrypt_failed` — usually means `FERNET_KEY` was rotated or replaced without re-encrypting the `api_keys` rows. Re-enter the key via Settings. |
| Run failed during engine execution | `grep run_service.engine_failed` (carries `run_id` in `extra`); the full traceback is in the `exc` field. |
| Background prune task crashed | `grep disk_pruner.tick_failed` for the traceback. The loop survives — the next tick still runs. |
| Ollama model dropdown is empty or out of date | `grep ollama_models.fetch_failed` for the underlying error. The service never raises; it returns the last-good cached list on failure, or `[]` on cold start. Check the `url` and `error` fields in the log line. Most often: `OLLAMA_BASE_URL` is wrong or `OLLAMA_API_KEY` doesn't match the endpoint. `curl -H "Authorization: Bearer $OLLAMA_API_KEY" $OLLAMA_BASE_URL/models` from the container reproduces in isolation. |
| Run failed with `model "X" not found` on Ollama | The picked model isn't in the live `/v1/models` for the configured endpoint. Pre-PR-0.2.5+hf.2 this was caused by the catalog listing local-Ollama tags against an Ollama Cloud endpoint; post-PR it's blocked at `POST /api/runs` pre-flight. If you see it now, the user has a stale browser tab — `/api/settings/defaults` auto-heal will clear it on next form load. |
| VPS shut down or hung at 100% CPU | See [VPS troubleshooting](#vps-troubleshooting) — correlate deploy vs first-run timing with `dmesg` OOM lines and whether local Ollama is on the same host. |

## Backup recommendations

| Asset | Why | How |
|---|---|---|
| Neon database | The runs / events / settings / login_attempts tables are the system of record. | Neon's built-in point-in-time snapshots are sufficient. Confirm the retention window in the Neon project settings. |
| `FERNET_KEY` | Without it the `api_keys` table cannot be decrypted and is dead weight. | Password manager. **Do not** store it in the same place as the Neon backups — losing both at once destroys the credential store with no recovery. |
| `<DATA_DIR>/memory/trading_memory.md` | The append-only cross-run decision log. Drives the "past decisions" context that the Portfolio Manager prompt consumes on every new run. This is the one file in the volume that cannot be regenerated. | Periodic copy off the volume (e.g. nightly `rsync` from inside the container, or a Coolify scheduled task). |
| `<DATA_DIR>/logs/` and `<DATA_DIR>/reports/` | Per-run reports + agent intermediates. Reproducible by re-running, but reproduction costs LLM tokens. | Optional. Most operators do not back these up. |
| `<DATA_DIR>/cache/checkpoints/` | Per-`(ticker, date)` LangGraph checkpoints. Used only for resume of an interrupted run. | Not needed. A successful run completes without ever reading its checkpoint, and the pruner reaps the file after `RETENTION_DAYS`. |

## Further reading

- [`../../DEPLOY.md`](../../DEPLOY.md) — Coolify setup walkthrough, DNS,
  TLS, volume permissions, VPS sizing, GHCR prebuilt images, alembic troubleshooting.
- [`architecture.md`](architecture.md) — how the runner, event bus,
  observer, and crash-recovery layer fit together.
- [`api.md`](api.md) — `/api/health` body shape, auth flow, SSE
  contract, rate-limit details.
