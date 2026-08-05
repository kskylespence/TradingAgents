# API reference

> Audience: API consumers (frontend developers, integration scripts,
> on-call engineers reading a curl trace).

This file covers the **semantics** of the `/api/*` surface: auth flow,
CSRF, rate limit, SSE contract, cancellation timing, resume contract,
error envelopes. For the **byte-level wire shapes** (request bodies,
response field types, query parameters), use FastAPI's auto-generated
OpenAPI UI.

## Auto-generated docs

When the backend is started with `DEBUG=1`, the FastAPI Swagger UI is
mounted at:

```
GET  /api/docs       # interactive Swagger UI
GET  /api/redoc      # ReDoc rendering
GET  /api/openapi.json
```

In production (`DEBUG=0`) those paths return 404. The schemas in
`web/backend/app/schemas.py` are the source of truth either way and are
mirrored into `web/frontend/src/lib/types.ts`.

## Base URL and path layout

All application routes live under `/api/*`. Anything else falls through
to the static SPA mount (`index.html` for unknown paths so React Router
can handle them).

| Prefix | Owner |
|---|---|
| `/api/auth` | `app/routers/auth.py` |
| `/api/catalog` | `app/routers/catalog.py` |
| `/api/runs` | `app/routers/runs.py` |
| `/api/history` | `app/routers/history.py` |
| `/api/settings` | `app/routers/settings.py` |
| `/api/health` | `app/routers/health.py` (public) |
| `/api/announcements` | `app/routers/announcements.py` |

### Catalog and validation invariants (added in 0.2.5+hf.2)

The catalog and run-submission paths enforce a four-layer defense to
keep the user from picking a provider/model that can't physically work
in the current deployment. New developers touching these endpoints
should know the invariants:

1. **`GET /api/catalog/providers` is filtered by env-credential
   presence** via `tradingagents.providers.available_providers()`. A
   provider with no env var set never appears in the dropdown. Ollama
   special case: present iff `OLLAMA_BASE_URL` is set. Azure: present
   iff both `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` are set.
2. **`GET /api/catalog/models?provider=ollama` is live-discovered**
   via `GET {OLLAMA_BASE_URL}/models` with a 5-minute TTL cache
   (`app/services/ollama_models.py`). Both local `ollama serve` and
   Ollama Cloud implement the OpenAI-compatible list endpoint, so the
   same call works for both. Static providers (openai, anthropic, etc.)
   still come from `tradingagents/llm_clients/model_catalog.py`.
3. **`GET /api/settings/defaults` auto-heals** stale saved model names
   by returning `null` when the saved `quick_think_llm` / `deep_think_llm`
   is not in the live catalog for the saved provider. The DB row is
   NOT mutated; the next PUT overwrites cleanly. When no `user_defaults`
   row exists yet, the response uses schema defaults: `research_depth=1`,
   `analysts=["market","social"]`, `llm_provider="ollama"`,
   `quick_think_llm` / `deep_think_llm` = `"glm-5.2"`,
   `enable_checkpoint=true`.
4. **`PUT /api/settings/defaults` validates** provider + model against
   the live catalog and returns 400 with the available list. `null` is
   accepted (that's how auto-heal stores its "I don't know" state).
5. **`POST /api/runs` validates** provider + model pre-flight before
   the engine launches, so stale tabs / curl clients bypassing the
   form get a fast descriptive 400 instead of a 10-second-late 500
   from inside the engine.

Providers whose static catalog includes a `"custom"` entry (deepseek,
openrouter, azure, etc.) get a synthetic `{id: "__custom__",
allows_custom: true}` terminal entry in `GET /api/catalog/models` —
the frontend swaps the dropdown for a text input when the user picks
it. **Ollama deliberately does NOT get a `__custom__` entry**: live
discovery is canonical, so accepting an arbitrary typed ID would just
reproduce the 404s that motivated this whole change.

## Auth flow

Cookie-based, single-user. Both cookies are set by `POST /api/auth/login`
and cleared by `POST /api/auth/logout`.

| Cookie | HttpOnly | Why |
|---|---|---|
| `access_token` | yes | HS256 JWT signed with `JWT_SECRET`. XSS cannot exfiltrate it. |
| `csrf_token` | no | 32-byte hex token. The SPA reads it from JS and echoes it as `X-CSRF-Token` on state-changing requests. |

Both cookies use `SameSite=Lax` and `Secure` when the original request
was HTTPS. The HTTPS check reads `request.url.scheme`, which uvicorn's
`ProxyHeadersMiddleware` rewrites from the trusted reverse-proxy hop
when the entrypoint runs uvicorn with `--proxy-headers
--forwarded-allow-ips='*'` (it does — see `entrypoint.sh`). The app
code does NOT parse raw `X-Forwarded-Proto` itself; that header is only
trusted via Starlette's middleware, which is what Coolify+Traefik
expects. TTL is controlled by `JWT_TTL_SECONDS` (default `604800` =
7 days); the cookie `max_age` matches.

**Rotation.** Changing `JWT_SECRET` invalidates every existing JWT
immediately — same as forcing a global logout. This is the
"log out everywhere" lever; document it for operators.

`GET /api/auth/me` is the canonical "am I logged in" probe — it returns
`{username}` on 200 or 401 on missing/expired JWT. The frontend uses it
on app boot to decide between `/login` and the routed shell.

## CSRF

Double-submit cookie pattern, implemented in
`web/backend/app/middleware/csrf.py`.

- Safe methods (`GET`, `HEAD`, `OPTIONS`) are never checked.
- For state-changing methods (`POST`, `PUT`, `DELETE`, `PATCH`), the
  middleware requires both:
  - the `csrf_token` cookie, AND
  - an `X-CSRF-Token` request header
  - with identical values.
- Mismatch or absence returns `403 {"detail": "CSRF token missing or invalid"}`.

**Exempt path: exactly `POST /api/auth/login`** (the path that *sets*
the cookie — requiring it on the same request would be a deadlock).
This is a frozen set, not a regex; adding a new exemption needs a
documented threat-model reason.

Logout (`POST /api/auth/logout`) is **not** exempt: it requires both
the JWT and a valid CSRF header (defense in depth).

The frontend `lib/api.ts` handles the header automatically — don't
hand-roll it in components.

## Login rate limit

Implemented in `web/backend/app/services/rate_limit.py`.

| Setting | Default |
|---|---|
| Window | 5 minutes |
| Failures per IP per window | 5 |
| Lockout response | `401 {"detail": "Too many login attempts"}` + `Retry-After: <seconds>` |

The client IP is taken from `request.client.host`, which is the
trusted post-`--proxy-headers` value populated by uvicorn from the
reverse proxy's rightmost `X-Forwarded-For` hop. The app code does NOT
parse raw `X-Forwarded-For` itself — doing so would let a direct
attacker spoof the leftmost value on every request and bypass the
limiter entirely. See `entrypoint.sh` for the uvicorn invocation.

Every attempt — success or failure — is persisted to the
`login_attempts` table. On process restart, the first `check()` per IP
hydrates the in-memory bucket from the DB so a deploy or crash does
**not** reset an ongoing lockout. A successful login clears the bucket
for that IP, so a typo-then-correct sequence isn't punished beyond the
initial wrong attempts.

## User administration

`GET`/`POST`/`DELETE /api/users` let an **admin** manage accounts from the
Settings page. All three take `Depends(require_admin)`, so a valid JWT with
`role: "user"` gets `403`, not `401`.

`UserSummary` (the only shape these endpoints return) has no `password` or
`password_hash` field, so credential material cannot leak through a response
body, an error payload, or a log line. It carries `run_count` so the UI can
explain a blocked delete instead of showing a bare `409`.

**Creating.** `CreateUserRequest` is `{username, password}` — there is no
`role` field, and new accounts are hardcoded to `role="user"` server-side.
Pydantic ignores unknown keys, so a client POSTing `role: "admin"` is silently
dropped rather than honoured; `tests/test_users_admin.py` asserts this
explicitly so a future `**body.model_dump()` refactor can't quietly open an
escalation path.

Validation:

| Field | Rule | Why |
|---|---|---|
| `username` | stripped, 3–128 chars | — |
| `username` | collisions rejected **case-insensitively** → `409` | The DB `UNIQUE` constraint is case-sensitive, so `Rob` and `rob` would otherwise coexist as two accounts that look identical in the UI |
| `password` | ≥ 8 chars | — |
| `password` | ≤ 72 **bytes** UTF-8 encoded → `422` | bcrypt hashes only the first 72 bytes. Bytes, not characters: 40 emoji is 160 bytes. Truncating would mean two different passwords authenticate the same account, so we reject instead |

**Deleting.** Four refusals, each with its own status so the UI can explain
itself:

| Case | Status |
|---|---|
| Target is the bootstrap admin (`BOOTSTRAP_ADMIN_ID`, `…0001`) | `400` — `upsert_admin_user()` re-creates it from env on every boot, so deleting it silently reappears |
| Target is the caller | `400` — prevents an admin locking themselves out |
| Target owns runs | `409`, message names the count |
| No such id | `404` |

The owns-runs check runs in **application code**, not via the database. `Run.user_id`
is `ForeignKey(..., ondelete="RESTRICT")`, so Postgres would raise — but SQLite
only enforces foreign keys when `PRAGMA foreign_keys=ON` is set per connection,
and the test suite runs in-memory SQLite. Checking explicitly keeps the behaviour
identical on both engines and lets the response carry the run count.

Runs are deliberately **not** cascaded. They are the product of the app;
destroying analysis history as a side effect of removing a login would be
surprising and unrecoverable.

> **Deleting a user does not revoke a session they already hold.** The JWT is
> stateless — `get_current_user` verifies the signature and expiry and never
> consults the database, and there is no revocation list. A deleted user's
> existing cookie therefore keeps working until it expires:
> `jwt_ttl_seconds` defaults to **604800 (7 days)**. During that window they
> can still reach JWT-only routes such as `GET /api/auth/me` and
> `GET/PUT /api/settings/defaults`.
>
> Their next `POST /api/runs` is worse than a clean rejection: the insert
> sets `Run.user_id` to a row that no longer exists, which on Postgres raises
> a foreign-key `IntegrityError` with no handler → **500**. On SQLite (dev)
> foreign keys aren't enforced, so it instead writes an orphaned run — the
> two engines diverge here.
>
> To cut access immediately, rotate `JWT_SECRET`, which invalidates every
> outstanding token. Closing this properly would mean checking user existence
> in `get_current_user` (a query per request) or adding a token-version
> column; both are out of scope for this change and neither is implemented.

## Run lifecycle

States and transitions (`status` column on `runs`, also the
`RunDetail.status` field on the wire):

```
                                      ┌──> completed   (engine returned, rating set)
                                      │
queued ──> running (lock acquired) ───┼──> failed      (engine raised)
                                      │
                                      ├──> cancelled   (cancel_event observed)
                                      │
                                      └──> interrupted (server crash during running)
```

Transitions per endpoint:

| Endpoint | Effect on status |
|---|---|
| `POST /api/runs` | inserts `queued`, spawns task; 409 if another run holds `GLOBAL_RUN_LOCK` |
| (lifecycle task) | `queued -> running` once lock acquired; eventually flips to one of the four terminal states |
| `POST /api/runs/:id/cancel` | sets the per-run `cancel_event`; the next chunk-boundary flip is `cancelled` |
| `POST /api/runs/:id/resume` | inserts a NEW `queued` run; the parent row stays `interrupted` |
| `POST /api/runs/:id/retry` | inserts a NEW `queued` run reusing the parent's persisted params; allowed only when parent is `failed` or `cancelled` |
| startup `crash_recovery` hook | any orphan `running` -> `interrupted`; emits a terminal `run_failed` event |

`interrupted` is the only state a run can be in without the lifecycle
task having published a terminal SSE event in the same process — the
crash-recovery startup hook backfills one so reconnecting clients see a
clean end-of-stream.

## SSE contract

`GET /api/runs/:id/events` returns a `text/event-stream` powered by
`sse-starlette`. Two phases inside the generator: replay from
`run_events` (rows with `seq > Last-Event-ID`), then live-tail from a
per-subscriber asyncio queue.

### Frame format

Each delivered frame is:

```
id: <seq>
data: <json payload>

```

`event:` is intentionally **not set** so the client receives the default
`message` event name; the frontend discriminates on the `type` field
inside the JSON payload (see the discriminated union in
`app/schemas.py`).

`seq` is a monotonic per-run integer assigned by the event bus inside a
per-run asyncio lock. Subscribers reconnect with `Last-Event-ID: <seq>`
and the bus replays everything with `seq > Last-Event-ID` before
re-attaching to the live tail.

### Heartbeats

`sse-starlette` emits an SSE comment frame every **15 seconds**:

```
: ping - <timestamp>

```

This keeps Coolify's Traefik (and any intermediate proxy) from idle-
closing the socket. The frontend ignores comments by design — they
exist only to keep the connection alive.

### Resume contract (SSE-level, not run-level)

If a client disconnects mid-stream, it stores the last `id:` it saw and
reconnects with `Last-Event-ID: <that seq>`. The bus replays every
persisted row strictly after that seq, then resumes live tailing. A
malformed `Last-Event-ID` (non-integer) is treated as 0 — i.e. full
replay.

### Stream termination

The generator exits cleanly when **either**:

1. it yields a terminal event (`run_completed`, `run_failed`, or
   `run_cancelled`), or
2. the client disconnects (the generator's `CancelledError` is
   swallowed and the per-subscriber queue is unregistered).

### Event taxonomy

Every payload includes `type` (the discriminator), `seq`, and a
server-assigned `ts` (ISO-8601 UTC). The table below lists the
type-specific fields. The Pydantic union lives in
`web/backend/app/schemas.py`.

| `type` | Payload fields (in addition to `seq`, `ts`) |
|---|---|
| `run_started` | `ticker`, `asset_type`, `analysis_date`, `analysts[]`, `research_depth`, `llm_provider`, `quick_think_llm`, `deep_think_llm`, `output_language`, `checkpoint_enabled`, `thinking_config?` |
| `agent_status` | `agent` (str), `status` (`pending` / `in_progress` / `completed` / `error`) |
| `progress_update` | `progress` (0.0–1.0), `step` (str) |
| `analyst_wall_time` | `key` (AnalystKey), `label` (str), `seconds` (float) |
| `tool_call` | `name` (str), `args` (dict), `timestamp` (str) |
| `message` | `kind` (`User`/`Agent`/`Data`/`Control`/`System`), `content` (str), `timestamp` (str). Uses `kind` because `type` is the union discriminator. |
| `report_section` | `section` (str — one of the known keys or arbitrary for forward-compat), `content` (str, markdown) |
| `investment_debate` | `bull?`, `bear?`, `judge?` (str) |
| `risk_debate` | `aggressive?`, `conservative?`, `neutral?`, `judge?` (str) |
| `stats` | `llm_calls`, `tool_calls`, `tokens_in`, `tokens_out`, `elapsed_seconds` |
| `run_completed` | `rating` (one of `Buy` / `Overweight` / `Hold` / `Underweight` / `Sell`), `report_dir` (str), `finished_at` (datetime) |
| `run_failed` | `error` (str) |
| `run_cancelled` | `at_node?` (str) |

### Back-pressure

Each subscriber gets its own queue with a max size of 200. If a
subscriber falls behind, `publish` drops the live frame for that
subscriber and logs `event_bus.queue_full_dropped_frame`. The DB row is
still persisted — the slow client recovers on its next reconnect via
`Last-Event-ID` replay.

## Cancellation timing

`POST /api/runs/:id/cancel` returns `204` immediately and sets the
per-run `cancel_event`. It is a no-op if the run is no longer active
(already terminal, or never started).

**Cancellation is cooperative at chunk boundaries.** The LangGraph
engine's stream loop polls `cancel_event.is_set()` between chunks; each
chunk is one node, and most nodes are a single LLM call. Real-world
cancellation latency is therefore **tens of seconds** in the worst case,
because a long deep-think model call has to finish before the loop
sees the flag.

**Mid-LLM cancel is NOT in v1.** Killing the underlying HTTP request
would require provider-specific transport plumbing we don't have. The
UI should show a "cancelling..." state and wait. Once the loop observes
the event, the terminal event becomes `run_cancelled` (not
`run_failed`), and the row flips to `cancelled`.

## Resume contract

`POST /api/runs/:id/resume` is allowed only when **all three** are true:

1. parent run `status == "interrupted"`,
2. parent run `checkpoint_enabled == true`,
3. the checkpoint file exists on disk at
   `<data_dir>/cache/checkpoints/<TICKER_UPPER>.db`.

Violation matrix:

| Condition | Response |
|---|---|
| Parent not found | `404 {"detail": "Run not found"}` |
| Parent not `interrupted` | `409 {"detail": "Cannot resume run in status '<status>'"}` |
| `checkpoint_enabled=False` | `409 {"detail": "Run was not checkpointed; cannot resume"}` |
| Another run is in progress | `409 {"detail": "Another run is in progress"}` (raised by `start_run`) |
| All three pass | `200 {"run_id": "...", "parent_run_id": "..."}` |

The new run inherits `(ticker, analysis_date)` from the parent. The
LangGraph `thread_id` is a hash of that pair, so the SqliteSaver
recognises the existing checkpoint and resumes from the last
successful node instead of starting from scratch. The frontend
navigates to the new `run_id` and subscribes to its SSE stream.

## Retry contract

`POST /api/runs/:id/retry` is the recovery affordance for runs that
ended **without** a usable checkpoint — typically because they died
inside the first analyst before any checkpoint was written, or because
`checkpoint_enabled=False` was used. It reconstructs a fresh
`RunRequest` from the parent row's persisted columns and delegates to
the same submit path as `POST /api/runs` (including
`_validate_models_against_catalog`).

Allowed only when parent `status in {"failed", "cancelled"}`.
`interrupted` already has `/resume`; `completed` / `running` / `queued`
are not retry-shaped.

Violation matrix:

| Condition | Response |
|---|---|
| Parent not found | `404 {"detail": "Run not found"}` |
| Parent not `failed`/`cancelled` | `400 {"detail": "Cannot retry run in status '<status>'"}` |
| Catalog validation fails (model removed, provider unconfigured) | `400` with the same envelope `POST /api/runs` returns |
| Another run is in progress | `409 {"detail": "Another run is in progress"}` (raised by `start_run`) |
| All pass | `200 {"run_id": "...", "parent_run_id": "..."}` |

Unlike `/resume`, the new run does NOT share a `thread_id` with the
parent — it starts the graph from scratch. That's the whole point:
the parent's state was either incomplete (no checkpoint) or
unrecoverable (transient upstream error), so we want a clean re-run
with the same params. The frontend navigates to the new `run_id` and
subscribes to its SSE stream.

The Retry button in `RunView.tsx` renders in two places when status is
`failed`/`cancelled`: the header alongside Cancel/Resume, and inside
the error-message banner so the affordance sits visually adjacent to
the failure reason.

## Report download

`GET /api/runs/:id/report?format=md|json|zip` — JWT required.

- 404 `"Run not found"` if the row is missing.
- 404 `"Run has not produced a report yet"` if `report_dir` is unset
  (the run hasn't completed).
- 404 `"Report directory missing on disk"` if the row points to a path
  that no longer exists (likely pruned by `disk_pruner`).

Format details:

| `format` | Content-Type | Body |
|---|---|---|
| `md` (default) | `text/markdown; charset=utf-8` | `report.md` verbatim, served via `FileResponse` |
| `json` | `application/json` | Synthesized envelope: `{run_id, ticker, analysis_date, rating, decision, report_markdown}` |
| `zip` | `application/zip` | The whole `report_dir` packed in-memory, `Content-Disposition: attachment` |

Anything outside the regex `^(md|json|zip)$` returns 422 from the
FastAPI query validation.

## Error envelopes

FastAPI's default: `{"detail": "<message>"}`. Common cases:

| Status | `detail` | When |
|---|---|---|
| 401 | `Not authenticated` | missing `access_token` cookie |
| 401 | `Token expired` | JWT past its `exp` |
| 401 | `Invalid token` / `Invalid token subject` | malformed JWT or missing `sub` |
| 401 | `Invalid credentials` | wrong username or password (deliberately generic so callers can't enumerate which) |
| 401 | `Too many login attempts` + `Retry-After` header | rate-limit lockout |
| 403 | `CSRF token missing or invalid` | state-changing request without a matching cookie/header pair |
| 404 | `Run not found` / `Run has not produced a report yet` / `Report directory missing on disk` | report download or run detail miss |
| 409 | `Another run is in progress` | second `POST /api/runs` while `GLOBAL_RUN_LOCK` is held |
| 409 | `Cannot resume run in status '<status>'` / `Run was not checkpointed; cannot resume` | resume preconditions failed |
| 400 | `Invalid cursor: <reason>` | malformed `cursor` query on `GET /api/history` |
| 400 | `Unknown provider env-var: '<env>'` | settings PUT/DELETE on an env name not in `PROVIDER_API_KEY_ENV` |
| 400 | `Model '<name>' is not available on provider '<key>'. Available: <list>` | `POST /api/runs` pre-flight or `PUT /api/settings/defaults` validation rejected a model that's not in the live catalog for that provider. The error inlines the available list so the caller knows what to pick. |
| 400 | `Provider '<key>' is not configured on this deployment. Available providers: <list>` | `POST /api/runs` was called with a provider whose credentials are not present in env. Adds defense-in-depth on top of the frontend's env-filtered provider dropdown. |
| 400 | `Cannot save <field>=<value> without an llm_provider. Include llm_provider in the PUT body.` | `PUT /api/settings/defaults` tried to set `quick_think_llm` / `deep_think_llm` without a provider to validate it against. |

FastAPI's own validation errors (422) follow Pydantic's
`{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` shape and
are documented under each endpoint in `/api/docs`.

## Endpoint reference card

| Method | Path | Auth | CSRF | Returns |
|---|---|---|---|---|
| `POST` | `/api/auth/login` | none | exempt | `204` + sets `access_token`, `csrf_token` cookies |
| `POST` | `/api/auth/logout` | JWT | required | `204` + clears cookies |
| `GET` | `/api/auth/me` | JWT | n/a | `AuthUser` |
| `GET` | `/api/catalog/providers` | JWT | n/a | `CatalogProvider[]` |
| `GET` | `/api/catalog/models?provider=&mode=` | JWT | n/a | `CatalogModel[]` |
| `GET` | `/api/catalog/analysts?asset_type=` | JWT | n/a | `CatalogAnalyst[]` |
| `GET` | `/api/catalog/languages` | JWT | n/a | `CatalogLanguage[]` |
| `POST` | `/api/runs` | JWT | required | `{run_id, status: "queued"}` |
| `GET` | `/api/runs/:id` | JWT | n/a | `RunDetail` |
| `GET` | `/api/runs/:id/events` | JWT (cookie) | n/a | SSE stream of `RunEvent` |
| `POST` | `/api/runs/:id/cancel` | JWT | required | `204` |
| `POST` | `/api/runs/:id/resume` | JWT | required | `{run_id, parent_run_id}` |
| `GET` | `/api/runs/:id/report?format=md\|json\|zip` | JWT | n/a | file or JSON envelope |
| `GET` | `/api/history?cursor=&limit=&ticker=&status=` | JWT | n/a | `HistoryPage` (keyset cursor) |
| `GET` | `/api/settings/api-keys` | JWT | n/a | `ApiKeyStatus[]` (never plaintext) |
| `PUT` | `/api/settings/api-keys/{env}` | JWT | required | `204` |
| `DELETE` | `/api/settings/api-keys/{env}` | JWT | required | `204` |
| `GET` | `/api/settings/defaults` | JWT | n/a | `UserDefaults` |
| `PUT` | `/api/settings/defaults` | JWT | required | `UserDefaults` (partial merge) |
| `GET` | `/api/users` | JWT (**admin**) | n/a | `UserSummary[]` (never a hash) |
| `POST` | `/api/users` | JWT (**admin**) | required | `201` + `UserSummary` |
| `DELETE` | `/api/users/{user_id}` | JWT (**admin**) | required | `204` |
| `GET` | `/api/health` | none | n/a | `HealthResponse` (`{status, version, db, disk_free_mb, active_run_id, ollama?}`), always HTTP 200 |
| `GET` | `/api/announcements/` | JWT | n/a | `Announcement[]` (cached proxy) |

`/api/health` returns `status: "ok"` when everything is healthy and
`status: "degraded"` with `db: "down"` when the DB probe fails. The
HTTP code is always 200 by design — Coolify treats non-2xx as
"restart the container," which is the wrong reaction to a transient DB
blip. See `web/docs/operations.md` for the rationale.

When `TRADINGAGENTS_LLM_PROVIDER == "ollama"` the response includes an
`ollama` block with the upstream probe state:

```json
"ollama": {
  "status": "ok" | "down" | "unknown",
  "url": "https://ollama.com/v1",
  "model_count": 39,        // int when status="ok"; null on "down"/"unknown"
  "error": null,             // repr() of the underlying exception on "down"
  "recent_attempts": [       // v0.2.5+hf.4 — rolling last-3 attempt log
    {"at": "2026-05-24T17:32:18+00:00", "ok": true,  "error": null},
    {"at": "2026-05-24T17:32:48+00:00", "ok": false, "error": "ConnectTimeout('')"},
    {"at": "2026-05-24T17:33:18+00:00", "ok": true,  "error": null}
  ],
  "circuit_state": "closed"  // v0.2.5+hf.4 — "closed" | "open" | "half_open"
}
```

`status` distinguishes:
- `"ok"` — last upstream probe succeeded. `model_count` may legitimately
  be `0` (an account with no models provisioned is still "ok").
- `"down"` — **2 of the last 3** probe attempts failed (hysteresis;
  v0.2.5+hf.4). `error` carries the underlying exception repr for ops
  triage. The OUTER `status` stays `"ok"` — same Coolify invariant as
  DB-down — so an upstream LLM outage does NOT restart the container.
- `"unknown"` — no probe attempted yet in this process. Rare in
  practice.

**Hysteresis (v0.2.5+hf.4).** A single transient (e.g. a 2-second
TCP RTT spike against `ollama.com/v1`) no longer flips `status` to
`"down"`. The user-visible alert only fires on sustained outages
(2-of-3). `recent_attempts` is the underlying log; the UI can show
a "last 3 polls" indicator by reading it directly.

**Circuit breaker (v0.2.5+hf.4).** `circuit_state` mirrors the shared
`upstream_http` breaker:

- `"closed"` — normal operation.
- `"open"` — 5+ consecutive failures detected; new requests
  short-circuit with `CircuitBreakerError` and the catalog falls back
  to last-good cache. Stays open for 30 s.
- `"half_open"` — cooldown elapsed; one trial probe is in flight. A
  success closes the circuit; a failure reopens it.

The frontend `<OllamaUpstreamAlert>` reads both fields and renders
three calibrated states (no alert / yellow "recovering" pill /
red "cool-down" or sustained-down alert).

The probe shares the catalog endpoint's TTL cache (300 s), so health
checks add no upstream load beyond what `/api/catalog/models` already
pays. Per-attempt status is tracked separately from the cache in
`app/services/ollama_models.py:_last_attempts` (a `deque[3]` per
`base_url`) — that's what drives the hysteresis above AND lets
`"ok with 0 models"` be distinguished from `"down with cold cache"`.

## See also

- `web/docs/architecture.md` — how the engine, event bus, and observer fit together (covers the SSE flow end-to-end).
- `web/docs/backend-dev.md` — adding a router; soft-auth pattern; security invariants.
- `web/docs/frontend-dev.md` — `useEventSource` / `useRun`; how the SPA consumes the SSE contract.
- `web/docs/operations.md` — env vars (`JWT_SECRET`, `FERNET_KEY`, `JWT_TTL_SECONDS`, ...), health states, secret rotation.
