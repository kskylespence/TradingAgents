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
| `ADMIN_PASSWORD_HASH` | bcrypt hash of the admin password (`passlib.hash.bcrypt`). The plaintext password is never stored. Generate with the recipe in [`DEPLOY.md` Step 1](../../DEPLOY.md). |
| `JWT_SECRET` | HMAC secret for signing the `access_token` cookie (HS256, `app/auth.py`). Rotating it invalidates every existing session — see [Secret rotation](#secret-rotation). 64 hex chars from `openssl rand -hex 32`. |
| `FERNET_KEY` | Master key used by `app/crypto.py` to encrypt provider API keys at rest in the `api_keys` table. 44 url-safe base64 chars from `Fernet.generate_key()`. **Lose this key and the stored keys become unreadable forever.** |
| `DATABASE_URL` | SQLAlchemy async URL. Production: `postgresql+asyncpg://USER:PASS@HOST.neon.tech/DB?ssl=require`. Local dev defaults to `sqlite+aiosqlite:///:memory:`. |

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
  "db":     "ok" | "down",
  "disk_free_mb": 18234,
  "active_run_id": null
}
```

| `status` | `db` | HTTP code | What it means |
|---|---|---|---|
| `ok` | `ok` | 200 | DB reachable on a trivial `SELECT 1`. Normal operation. |
| `degraded` | `down` | 200 | DB unreachable. The app keeps serving static assets and login screen so a human can see the failure. |

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
  TLS, volume permissions, alembic troubleshooting.
- [`architecture.md`](architecture.md) — how the runner, event bus,
  observer, and crash-recovery layer fit together.
- [`api.md`](api.md) — `/api/health` body shape, auth flow, SSE
  contract, rate-limit details.
