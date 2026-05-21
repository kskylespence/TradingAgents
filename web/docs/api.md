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

## Auth flow

Cookie-based, single-user. Both cookies are set by `POST /api/auth/login`
and cleared by `POST /api/auth/logout`.

| Cookie | HttpOnly | Why |
|---|---|---|
| `access_token` | yes | HS256 JWT signed with `JWT_SECRET`. XSS cannot exfiltrate it. |
| `csrf_token` | no | 32-byte hex token. The SPA reads it from JS and echoes it as `X-CSRF-Token` on state-changing requests. |

Both cookies use `SameSite=Lax` and `Secure` when the original request
was HTTPS (the middleware honors `X-Forwarded-Proto` so cookies behave
correctly behind Coolify's Traefik). TTL is controlled by
`JWT_TTL_SECONDS` (default `604800` = 7 days); the cookie `max_age`
matches.

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

The client IP is taken from the leftmost `X-Forwarded-For` entry first
(set by Coolify / Traefik) and falls back to `request.client.host`.

Every attempt — success or failure — is persisted to the
`login_attempts` table. On process restart, the first `check()` per IP
hydrates the in-memory bucket from the DB so a deploy or crash does
**not** reset an ongoing lockout. A successful login clears the bucket
for that IP, so a typo-then-correct sequence isn't punished beyond the
initial wrong attempts.

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
| `GET` | `/api/health` | none | n/a | `{status, db, disk_free_mb, active_run_id}`, always HTTP 200 |
| `GET` | `/api/announcements/` | JWT | n/a | `Announcement[]` (cached proxy) |

`/api/health` returns `status: "ok"` when everything is healthy and
`status: "degraded"` with `db: "down"` when the DB probe fails. The
HTTP code is always 200 by design — Coolify treats non-2xx as
"restart the container," which is the wrong reaction to a transient DB
blip. See `web/docs/operations.md` for the rationale.

## See also

- `web/docs/architecture.md` — how the engine, event bus, and observer fit together (covers the SSE flow end-to-end).
- `web/docs/backend-dev.md` — adding a router; soft-auth pattern; security invariants.
- `web/docs/frontend-dev.md` — `useEventSource` / `useRun`; how the SPA consumes the SSE contract.
- `web/docs/operations.md` — env vars (`JWT_SECRET`, `FERNET_KEY`, `JWT_TTL_SECONDS`, ...), health states, secret rotation.
