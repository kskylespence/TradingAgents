# CLAUDE.md — `web/frontend/`

Authoritative conventions for Claude Code sessions editing under
`web/frontend/`. Inherits from the repo-root `CLAUDE.md`; overrides
when they conflict.

## What this subtree is

Vite + React 18 + TypeScript 5 + Tailwind 3 + shadcn/ui SPA. Talks to
`/api/*` on the FastAPI backend. In dev, Vite proxies `/api` to
`:8000`. In production, FastAPI's static mount serves the built bundle
from `web/backend/app/static/` and the SPA shares the origin with the
API.

## Per-route file ownership

`src/routes/{Login, NewRun, RunView, History, Settings}.tsx` — each
route owns its file. Parallel work on different routes doesn't
conflict because `src/App.tsx` already lists all five routes and is
treated as final.

To add a new route: drop `src/routes/MyThing.tsx`, then add a `lazy()`
import and a `<Route>` in `App.tsx`. `App.tsx` IS the one shared file
new-route work touches.

## Files you must NOT modify casually

- `src/App.tsx` — only edit when adding a top-level route or changing
  the navbar/layout.
- `src/main.tsx` — entry point, never changes.
- `src/lib/types.ts` — this is the backend↔frontend type contract. It
  mirrors `web/backend/app/schemas.py`. If you change it without
  updating the Pydantic schema (or vice versa), you've broken the
  contract.
- `src/components/ui/*` — shadcn primitives. Regenerate via
  `npx shadcn add <component>`, don't hand-edit.

## The two SSE layers

- **`src/lib/sse.ts:useEventSource(url, opts)`** is low-level. Returns
  the raw growing `events: RunEvent[]` array, `lastSeq`, connection
  state. No domain logic. Don't put run-specific reducer code here.
- **`src/hooks/useRun.ts:useRun(runId)`** is the reducer over those
  events plus a React Query fetch for the baseline `RunDetail`. Every
  `RunEvent.type` is a `case` in the reducer's switch with an
  exhaustiveness `_exhaustive: never` guard — TypeScript will flag a
  missing case if you add a new event type to `lib/types.ts`.
- The reducer uses `useReducer` + a `processedRef` to dispatch only NEW
  events on each render. Cost per event is O(1), not O(N) over the
  whole history.
- Tests for the reducer live in `src/__tests__/useRun.reducer.test.ts`
  and import `_reducer_for_tests` + `_applyEvent_for_tests` (exported
  exactly for testability).

## CSRF on state-changing requests

`src/lib/api.ts` reads the `csrf_token` cookie and sets the
`X-CSRF-Token` header automatically on `POST`/`PUT`/`DELETE`/`PATCH`.
**Don't roll your own** — if you find yourself reaching into
`document.cookie`, use `api.post(...)` / `api.put(...)` instead.

## Catalog-driven forms

Every dropdown in `NewRun.tsx` is hydrated from `/api/catalog/*` via
`src/hooks/useCatalog.ts`. Don't hardcode provider lists, model lists,
analyst keys, or language options anywhere in the frontend — the
backend is the single source of truth.

Wire-key vs label gotcha: the engine uses `"social"` as the wire key
for what the user sees as `"Sentiment Analyst"`. Render labels, POST
keys. The catalog endpoint already returns the right pair.

## Form pre-fill

`src/hooks/useUserDefaults.ts` calls `GET /api/settings/defaults` and
returns the user's saved form defaults. Use this for any field that
should remember the user's last choice.

## Dev loop without API keys

Set `FAKE_LLM=1` in the backend env. Runs finish in ~0.3s with a
canned `Buy` rating. The Playwright e2e relies on this; manual dev
testing also benefits.

## Testing

- **Vitest** in `src/__tests__/`. Use `vi.mock` to stub hooks/api at
  the module boundary. The `RunView.resume.test.tsx` pattern is the
  reference: mock `react-router-dom`'s `useNavigate` (and only that
  export) via `vi.importActual` + `useNavigate: () => navigateSpy`, so
  `<MemoryRouter>` still works normally.
- **Playwright** e2e in `e2e/`. `playwright.config.ts:webServer`
  boots uvicorn + Vite; `e2e/globalSetup.ts` creates `.pw-data`. Run
  with `npx playwright test`. Single worker because the backend has a
  global run lock.
- **Red-green discipline** for regression tests: write the test,
  revert the fix, confirm the test fails, restore the fix, confirm it
  passes. A regression test that's only ever been seen green could be
  passing for the wrong reasons.

## Bundle-size watch

`src/components/ReportPanel.tsx` pulls in `react-markdown` +
`remark-gfm`. This adds ~50 KB gz to the RunView chunk. Acceptable for
an internal admin UI. If we ever ship this publicly, code-split with
`lazy(() => import('react-markdown'))` so initial-page weight stays
small.

## Where the long-form docs live

- `web/docs/frontend-dev.md` — patterns for adding routes/components/hooks
- `web/docs/architecture.md` — how the frontend talks to the backend
- `web/docs/api.md` — the `/api/*` surface the frontend consumes
- `web/docs/testing.md` — vitest + Playwright in depth
