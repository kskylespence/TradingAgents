# Frontend development

> Audience: contributors adding code under `web/frontend/`. Read
> [`architecture.md`](architecture.md) first.

This guide covers the four most common frontend tasks (add a route, add
a component, consume the SSE stream, talk to `/api/*`) plus the
contracts you have to respect to avoid breaking the rest of the SPA.

For test patterns (vitest, Playwright, red-green discipline) see
[`testing.md`](testing.md). For the API surface you're consuming see
[`api.md`](api.md). For the dense rules-only version of this doc see
[`../frontend/CLAUDE.md`](../frontend/CLAUDE.md).

## Project layout

```
web/frontend/
├── src/
│   ├── App.tsx              # route table + providers (the one shared file)
│   ├── main.tsx             # entry point
│   ├── routes/              # one file per page
│   │   ├── Login.tsx
│   │   ├── NewRun.tsx
│   │   ├── RunView.tsx
│   │   ├── History.tsx
│   │   └── Settings.tsx
│   ├── components/          # reusable pieces; ui/ holds shadcn primitives
│   ├── hooks/               # useAuth, useCatalog, useHistory, useRun, useUserDefaults
│   ├── lib/                 # api.ts (fetch), sse.ts (EventSource), types.ts (backend contract), utils.ts
│   └── __tests__/           # vitest
└── e2e/                     # Playwright specs + globalSetup
```

Each `src/routes/*.tsx` owns its file. Multiple contributors can work
on different routes in parallel without conflict because `App.tsx`
already lists all of them — it's the one shared file new-route work
touches.

## Catalog-driven forms

Every dropdown in `NewRun.tsx` — providers, models, analysts,
languages — is hydrated live from `/api/catalog/*` via
`src/hooks/useCatalog.ts`. The backend is the single source of truth.
Adding a provider or model server-side flows into the UI for free.

```tsx
import { useProviders, useModels } from "@/hooks/useCatalog";

function ProviderPicker() {
  const providers = useProviders();
  const models = useModels(selectedProvider, "quick");
  // providers.data: CatalogProvider[]; cached 5 minutes, no refetch on focus.
}
```

**Wire-key vs label gotcha.** The engine uses `"social"` as the wire
key for what the user sees as `"Sentiment Analyst"`. Render labels,
POST keys. The catalog endpoint already returns the right pair on each
`CatalogAnalyst` (see `src/lib/types.ts`).

## The two SSE layers

The SSE plumbing is deliberately split in two so domain logic stays
out of the network layer.

- **`src/lib/sse.ts:useEventSource(url, opts)`** — low-level. Opens
  an `EventSource`, parses each frame as JSON into a `RunEvent`,
  appends to a growing array. Returns `{events, lastSeq, state,
  error, close}`. No reducer, no domain knowledge. Don't put
  run-specific logic here.
- **`src/hooks/useRun.ts:useRun(runId)`** — the reducer over those
  events plus a React Query fetch for the baseline `RunDetail`.
  Returns derived UI state (agents by name, message log, report
  sections, debate snapshots, stats, progress, final rating). This is
  what every route should consume.

### The reducer contract

Every variant of the `RunEvent` discriminated union (`src/lib/types.ts`)
is a `case` in the reducer's `switch`, with an exhaustiveness guard at
the end:

```ts
default: {
  const _exhaustive: never = ev;
  void _exhaustive;
  break;
}
```

Add a new event type to `types.ts` and TypeScript will flag the
missing case at the `_exhaustive: never` assignment. That is the only
mechanism keeping the frontend in sync with the backend's event
taxonomy — don't disable it.

### Incremental dispatch

`useRun` uses `useReducer` + a `processedRef` to dispatch only the
**new** slice of events on each render:

```ts
const newEvents = sse.events.slice(processedRef.current);
if (newEvents.length > 0) {
  dispatch({ type: "apply", events: newEvents });
  processedRef.current = sse.events.length;
}
```

Cost per event is O(1), not O(N) over the whole history. An earlier
`useMemo(() => reduceEvents(sse.events))` implementation re-reduced
the entire history on every new frame and caused visible jank on long
runs (200+ events). Keep the incremental pattern.

When `useEventSource` clears its event array (URL change, reconnect),
`processedRef.current > sse.events.length` and the reducer is reset.

## Adding a new component

Drop a `.tsx` file in `src/components/`. Naming convention:
`PascalCase.tsx` matching the default export. Import shadcn primitives
from `@/components/ui/*` — do **not** hand-edit those; they're
regenerated via `npx shadcn add <component>`.

```tsx
// web/frontend/src/components/MyThing.tsx
import { Card, CardContent } from "@/components/ui/card";

export function MyThing({ label }: { label: string }) {
  return <Card><CardContent>{label}</CardContent></Card>;
}
```

## Adding a new route

Two steps. Drop the route file in `src/routes/`, then wire it in
`App.tsx`:

```tsx
// src/App.tsx
const MyThing = lazy(() => import("@/routes/MyThing"));

<Route
  path="/my-thing"
  element={
    <ProtectedRoute>
      <Layout><MyThing /></Layout>
    </ProtectedRoute>
  }
/>
```

`<ProtectedRoute>` enforces auth (redirects to `/login` on 401);
`<Layout>` provides the navbar + announcement banner. Lazy imports
keep the route's bundle out of the initial chunk.

`App.tsx` is the one shared file new-route work touches. Keep your
edit to the routes table — don't restructure the providers or layout
in the same commit.

## CSRF on state-changing requests

`src/lib/api.ts` handles CSRF automatically. On any `POST`/`PUT`/
`PATCH`/`DELETE`, it reads the `csrf_token` cookie and sets the
`X-CSRF-Token` header (double-submit cookie pattern, matched on the
backend by `app/middleware/csrf.py`). The JWT cookie is sent via
`credentials: "include"`.

```tsx
const mutation = useMutation({
  mutationFn: (body: RunRequest) => api.post<{run_id: string}>("/api/runs", body),
  onSuccess: (data) => navigate(`/runs/${data.run_id}`),
});
```

If you find yourself reading `document.cookie` directly, stop — use
`api.post(...)` / `api.put(...)` from `@/lib/api` instead. Rolling
your own CSRF is how the contract drifts.

## React Query patterns

Query keys used across the app:

| Key | Hook | Invalidate after |
|---|---|---|
| `["catalog", ...]` | `useCatalog.ts` | Never — backend catalog is session-immutable |
| `["settings", "defaults"]` | `useUserDefaults.ts` | `PUT /api/settings/defaults` |
| `["api-keys"]` | (Settings page) | `PUT/DELETE /api/settings/api-keys/:env` |
| `["run", runId]` | `useRun.ts` | Auto: terminal SSE event triggers `runQuery.refetch()` |
| `["history", filters]` | `useHistory.ts` | After cancel/resume |
| `["health"]` | `useHealth.ts` | Auto-poll every 30 s (`refetchInterval: 30_000`) |

`useRun` refetches `["run", runId]` automatically when the reducer
sees a terminal event (`run_completed`, `run_failed`, `run_cancelled`)
so persisted fields (`rating`, `finished_at`, `stats`, `report_dir`)
line up with what the live stream just told the user.

`useHealth` polls `/api/health` at 30-second intervals with
`retry: 1` and no refetch-on-window-focus. It is consumed today only
by `NewRun.tsx` to drive the `<OllamaUpstreamAlert>` warning when
the user has Ollama selected but the backend probe reports `down`.
The alert suppresses on `unknown` (cold-start; no probe yet) and on
`ok` (including `ok with model_count: 0`) to avoid alert fatigue.

### Surfacing a pure-presentational warning

When a small piece of UI is just "show / hide based on a few props",
extract it into a component under `src/components/` and unit-test it
directly rather than mounting the consuming route. `NewRun.tsx`'s
inline Ollama-down alert started as a JSX block in the route and was
later extracted to `<OllamaUpstreamAlert>` because testing the route
end-to-end was prohibitively expensive (NewRun has 10+ `useEffect`s
and three Radix Select trees that combine to hang jsdom). The
extracted-component approach: `OllamaUpstreamAlert.tsx` is one
function, takes `{provider, health}`, returns `null` or a `<div
role="alert">`. Its test file mounts just the component with various
prop shapes — 3 s and 7 assertions vs. a multi-minute hang.

## Form pre-fill via user defaults

`src/hooks/useUserDefaults.ts` wraps `GET /api/settings/defaults`.
`NewRun.tsx` uses it to remember the user's last provider / model /
research depth / analyst set / language across visits. The pattern is
"apply once when both the defaults and the catalog have arrived,
fall back to the first catalog entry if the saved value isn't
available." Mirror it for any new field that should remember the
user's last choice.

## Dev loop without API keys

Set `FAKE_LLM=1` in the backend env before `uvicorn`:

```bash
cd web/backend
export FAKE_LLM=1
uvicorn app.main:app --reload --port 8000
```

Every run completes in ~0.3 seconds with a canned `Buy` rating, no
API keys required. The full SSE stream still fires
(`agent_status`, `report_section`, `stats`, `run_completed`), so you
can develop the live dashboard end-to-end without spending a token.
This is also what the Playwright e2e relies on.

## Backend ↔ frontend type contract

`src/lib/types.ts` mirrors `web/backend/app/schemas.py`. They are the
contract between server and SPA. **Changing one without the other
breaks the contract** — the frontend will silently parse missing
fields as `undefined`, or the backend will reject your `RunRequest`
with a 422.

When you need a new field on a request/response or a new variant on
`RunEvent`, the order is:

1. Backend Pydantic schema change (see
   [`backend-dev.md`](backend-dev.md#database-changes) — the same
   discipline applies to schema-only changes).
2. Alembic revision if it touches the DB.
3. TS mirror in `src/lib/types.ts`.
4. If you added a `RunEvent` variant, TypeScript will flag the missing
   reducer case in `useRun.ts` — handle it.

## Bundle-size watch

`src/components/ReportPanel.tsx` pulls in `react-markdown` +
`remark-gfm`, adding ~50 KB gz to the RunView chunk. Acceptable for
an internal admin tool. If this UI ever ships publicly, code-split
with `lazy(() => import('react-markdown'))` so the initial page
weight stays small.

## Where to look when stuck

When you're not sure how to structure something, the existing routes
and hooks are short and focused — copy their patterns:

| Pattern | Reference |
|---|---|
| Catalog-driven form with defaults | `src/routes/NewRun.tsx` |
| Live SSE-driven dashboard | `src/routes/RunView.tsx` + `src/hooks/useRun.ts` |
| Paginated list with filters | `src/routes/History.tsx` + `src/hooks/useHistory.ts` |
| Mutation that hits the backend | `src/routes/Settings.tsx` (API-key save) |
| Auth-gated route wrapper | `src/components/ProtectedRoute.tsx` |
| Reducer with exhaustiveness guard | `src/hooks/useRun.ts:applyEvent` |

## Next reads

- [`architecture.md`](architecture.md) — how the SPA and FastAPI fit together
- [`api.md`](api.md) — `/api/*` reference + SSE event taxonomy
- [`backend-dev.md`](backend-dev.md) — when a frontend change needs a backend change
- [`testing.md`](testing.md) — vitest + Playwright + red-green discipline
- [`operations.md`](operations.md) — env vars + runbook
- [`../frontend/CLAUDE.md`](../frontend/CLAUDE.md) — the rules-only version of this doc
