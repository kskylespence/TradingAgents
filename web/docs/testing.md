# Testing

> Audience: anyone writing or modifying code under `web/`. Read this
> before you write your first test in this subtree.

The Web UI has three test suites — backend `pytest`, frontend `vitest`,
and browser-driven Playwright `e2e`. They all run locally without real
LLM credentials thanks to the `FAKE_LLM=1` hook described below.

For extension-pattern context, see [`backend-dev.md`](backend-dev.md)
and [`frontend-dev.md`](frontend-dev.md).

## Where the tests live

| Suite | Location | Runner |
|---|---|---|
| Backend unit + integration | `web/backend/tests/` | `pytest` (asyncio-auto) |
| Frontend unit | `web/frontend/src/__tests__/` | `vitest` + `@testing-library/react` |
| Browser e2e | `web/frontend/e2e/` | `@playwright/test` |

The backend `tests/` directory is flat: one file per router or service
(`test_auth.py`, `test_history.py`, `test_runs_smoke.py`,
`test_event_replay.py`, …). The frontend mirrors the source layout —
hooks tested next to where they live in `__tests__/`.

## Backend pytest

`web/backend/pyproject.toml` pins the suite:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers -p no:postgresql"
asyncio_mode = "auto"
markers = ["unit", "integration", "smoke"]
```

`-p no:postgresql` disables the `pytest-postgresql` plugin by default.
The plugin auto-loads via setuptools entry points — even for tests that
never touch Postgres — and imports `psycopg`, which needs libpq at
*collection* time. Any host without a system libpq aborts the whole
session before a single test runs; that is Windows, and equally macOS
without Homebrew. The `dev` extra therefore also declares
`psycopg[binary]`, which vendors libpq into the wheel, so neither the
backend suite nor the repo-root suite depends on a system install.
Tests that need real Postgres re-enable the plugin via their own conftest.

`pytest-asyncio` is pinned `<1.0` in `web/backend/pyproject.toml`. The
1.x per-test loop and fixture-finalisation changes break
`test_rate_limit.py` (`RuntimeError: There is no current event loop in
thread 'MainThread'`) and three `test_runs_preflight_probe.py` tests.
The `pytest` major version is not the trigger — 8.x and 9.x behave
identically. Don't lift the cap without migrating to the 1.x loop-scope
API first.

```bash
cd web/backend
pytest                                                # full suite
pytest -m unit                                        # only unit-marked
pytest -m "not integration"                           # skip integration
pytest tests/test_runs_smoke.py                       # one file
pytest tests/test_runs_smoke.py::test_post_run_returns_id_and_completes  # one test
```

### Autouse fixtures (`tests/conftest.py`)

Three things happen before any test imports `app.config`:

1. **Backend env defaults** are set at import time (not in a fixture)
   because `get_settings()` is `@lru_cache`d. `DATABASE_URL` →
   in-memory SQLite, `FERNET_KEY` → a fresh `Fernet.generate_key()`,
   `JWT_SECRET` and `ADMIN_*` to placeholder values.
2. **Dummy provider API keys** for every supported provider via the
   autouse `_dummy_api_keys` fixture — same pattern as the parent
   repo's `tests/conftest.py`. Adding a new provider means adding its
   env var to `_API_KEY_ENV_VARS` here.
3. **`db_session` async fixture** yielding a session bound to a fresh
   in-memory SQLite engine with all tables created. Use it when your
   test only needs a DB session and not the full HTTP app.

### Minimal test template

```python
# web/backend/tests/test_widgets.py
from fastapi.testclient import TestClient


def test_widgets_list_returns_200() -> None:
    from app.main import app
    with TestClient(app) as client:
        resp = client.get("/api/widgets")
    assert resp.status_code == 200
```

Mirror `tests/test_foundation_smoke.py` for the simplest patterns and
`tests/test_history.py` for a router-with-DB pattern.

## The `FAKE_LLM=1` hook

`app/services/run_service.py` checks `os.environ.get("FAKE_LLM") == "1"`
and short-circuits `_run_engine` to `_fake_stream_run`, a scripted
observer-driven simulator that completes in ~0.3 seconds and always
emits `Rating: Buy`. It is the entire reason `test_runs_smoke.py` and
the Playwright e2e can exercise the full lifecycle without API keys.

Enable it in a test via `monkeypatch`:

```python
@pytest.fixture(autouse=True)
def _enable_fake_llm(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "1")
```

Use this instead of monkeypatching `stream_run` directly — the fake
emits the same callback sequence the real engine does, so it tests the
observer/event-bus path end to end.

## Soft-auth in tests

Auth itself is covered by `tests/test_auth.py`. For every other router
test, stub the dep:

```python
from uuid import UUID

from app.auth import get_current_user
from app.schemas import AuthUser

def _override_user() -> AuthUser:
    return AuthUser(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        username="test",
        role="admin",
    )

app.dependency_overrides[get_current_user] = _override_user
```

All three fields are required — `AuthUser` gained `id` and `role` with the
multi-user change, so a bare `AuthUser(username="test")` now raises
`ValidationError`. `tests/helpers.py:make_auth_user` builds one for you with
sensible defaults; prefer it over hand-rolling.

Pattern lifted from `tests/test_runs_smoke.py` and `tests/test_history.py`.
Always pop the override in a `finally:` so it doesn't leak to other
tests.

### Testing admin-gated routes

For routes behind `Depends(require_admin)`, override **`get_current_user`,
not `require_admin`**:

```python
app.dependency_overrides[get_current_user] = lambda: make_auth_user(role="user")
assert client.get("/api/users").status_code == 403
```

`dependency_overrides` substitutes a dependency *anywhere it appears in the
tree*, including nested inside `require_admin`. So overriding the inner one
leaves the real guard running and the 403 is genuinely produced by
production code. Overriding `require_admin` itself would replace the thing
under test with a stub, and the assertion would prove nothing.

Two refinements worth copying from `tests/test_users_admin.py`:

- **Aim the forbidden request at a target that would otherwise succeed.** Its
  non-admin `DELETE` probe targets an id the handler would act on, so a
  bypass would show up as a 2xx/4xx *other than* 403 — the assertion is
  load-bearing rather than incidentally true.
- **Drop the override entirely** (`dependency_overrides.pop`) to exercise the
  real cookie-reading `get_current_user` and assert the 401 path.

## CSRF disable in tests

`TestClient` doesn't carry the `csrf_token` cookie or the
`X-CSRF-Token` header, so any state-changing route 403s by default.
Two options:

1. **Monkey-patch the predicate** when CSRF isn't what you're testing
   (the `test_runs_smoke.py::client` pattern):
   ```python
   import app.middleware.csrf as csrf_mod
   orig = csrf_mod._csrf_required
   csrf_mod._csrf_required = lambda method, path: False  # type: ignore[assignment]
   try:
       ...
   finally:
       csrf_mod._csrf_required = orig
   ```
2. **Send the cookie/header pair** when CSRF *is* what you're testing
   (the `test_logout_clears_cookies` pattern):
   ```python
   csrf = client.cookies.get("csrf_token")
   resp = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
   ```

## SQLite + UUID gotcha

The Web UI uses `String(36).with_variant(UUID, "postgresql")` for run
ids (real `UUID` in prod, 36-char string in SQLite dev/test). aiosqlite
**cannot bind a raw `uuid.UUID` instance** to the SQLite column —
coerce to `str()` before insert.

```python
run_id = uuid.uuid4()
row = Run(id=str(run_id), ...)   # str(), not the raw UUID
```

References: `tests/test_history.py::seeded_runs`,
`tests/test_runs_smoke.py::test_resume_happy_path_returns_new_run_id`.

## Module-level lock reset pattern

pytest-asyncio gives each test its own event loop. Any `asyncio.Lock`
created at import time binds to the *first* loop it touches and crashes
in the second test with
`RuntimeError: <Lock> is bound to a different event loop`. Backend
code therefore uses the lazy `_get_lock()` pattern (see
[`backend-dev.md`](backend-dev.md)), and tests must reset that lock
between runs:

```python
# In a fixture for any test that touches the event_bus directly:
from app.services import event_bus as eb_mod
eb_mod.reset_for_tests()
eb_mod._lock = asyncio.Lock()   # public helper doesn't yet cover this
```

`login_rate_limiter.reset()` (called in `tests/test_auth.py`) is the
same idea for the rate limiter — its public `reset()` now drops the
lazy lock too.

Anywhere you write a new `_get_lock()` helper, give it a matching
`reset_for_tests()` that also nulls the lock.

### Resetting service caches between tests

The `ollama_models` service caches both a model list (`_cache`) and a
per-attempt outcome log (`_last_attempt`); both need to be cleared
between tests so probe-status assertions don't bleed across. The
backend conftest does this for you via an autouse fixture:

```python
# Already in web/backend/tests/conftest.py — no per-file boilerplate needed
@pytest.fixture(autouse=True)
async def _reset_ollama_cache():
    from app.services import ollama_models
    ollama_models._reset_for_tests()
    yield
    await ollama_models.drain_in_flight_refreshes()
    ollama_models._reset_for_tests()
```

`_reset_for_tests()` nulls the lock AND clears both dicts. If you add
a new service module with its own caches, mirror the pattern: expose
a `_reset_for_tests()`, register it in conftest as autouse.

**The fixture is `async` on purpose.** `list_ollama_models()`'s
stale-while-revalidate path fire-and-forgets an `asyncio.Task`, and a
sync fixture can only call `task.cancel()` — which merely *requests*
cancellation, leaving the task in state `cancelling` until the loop
runs it again. pytest-asyncio closes the per-test loop first, so the
task dies pending and the test reports `ERROR at teardown` /
`RuntimeError: Event loop is closed`. Only an `async` teardown can
`await` the task to completion. An autouse async fixture is safe for
this suite's sync tests too (verified against `test_runs_smoke.py`).

If you write a test that monkeypatches `drain_in_flight_refreshes`
itself, call `monkeypatch.undo()` before the test ends — this fixture's
teardown awaits that same function, and a patched-in exception turns a
passing test into an ERROR at teardown. See
`test_lifespan_upstream_warmup.py::test_shutdown_closes_client_even_if_drain_raises`.

## Mocking outbound HTTP — `install_fake_httpx_ollama`

When a router or service test exercises a path that fans out to
`ollama_models.list_ollama_models()`, monkeypatch `httpx.AsyncClient`
via the shared helper in conftest:

```python
from .conftest import install_fake_httpx_ollama

def test_routerwithollama(monkeypatch):
    record = install_fake_httpx_ollama(
        monkeypatch,
        ids=["gpt-oss:120b", "qwen3-coder:480b"],
    )
    # ... hit /api/catalog/models?provider=ollama (or /api/health, /api/runs) ...
    assert record["calls"] == 1
    assert record["last_url"].endswith("/models")
    assert "Bearer" in record["last_headers"]["Authorization"]
```

Use the shared helper for router-level tests. The service-level test
files (`test_ollama_models_service.py`,
`test_ollama_models_failure_keeps_last_good.py`) keep their own
ad-hoc fake clients because they need full control of the JSON
payload to test malformed-item edge cases and multi-step
success-then-failure scripts — that's a deliberate exception, not the
default.

## Vitest (frontend unit)

Layout: every test lives in `src/__tests__/` and ends in `.test.ts` or
`.test.tsx`. Scripts from `package.json`:

```bash
cd web/frontend
npm test                                              # vitest run
npm run test:watch                                    # vitest --watch
npx vitest run src/__tests__/sse.test.ts              # one file
```

### `vi.mock` at the module boundary

Stub a single export while keeping the rest real with `vi.importActual`
(`src/__tests__/RunView.resume.test.tsx`):

```ts
const navigateSpy = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  );
  return { ...actual, useNavigate: () => navigateSpy };
});
```

`MemoryRouter`, `Routes`, `Route` keep working; only `useNavigate` is
replaced. The same pattern stubs `@/lib/api` to return canned responses
while still using the real type definitions.

### Reducer testing without React

`src/hooks/useRun.ts` exports `_reducer_for_tests` and
`_applyEvent_for_tests` exactly so the reducer can be unit-tested
without React rendering or the SSE lifecycle:

```ts
import {
  _applyEvent_for_tests as applyEvent,
  _reducer_for_tests as reducer,
} from "@/hooks/useRun";

const initial = () => reducer(undefined as never, { type: "reset" });
const next = reducer(initial(), { type: "apply", events: [event] });
```

Every `RunEvent.type` should have a matching case in
`src/__tests__/useRun.reducer.test.ts`. The compiler's exhaustiveness
check (the `_exhaustive: never` guard in the reducer) flags missing
cases at build time; the test catches semantic regressions.

### FakeEventSource for the SSE hook

`src/__tests__/sse.test.ts` builds a controllable `FakeEventSource`
class, stubs it onto `globalThis` via `vi.stubGlobal("EventSource",
FakeEventSource)`, and drives lifecycle by hand (`emitOpen`,
`emitJsonMessage`, `emitErrorClosed`). Reuse this pattern for any new
SSE-related hook test.

### Avoid mounting heavyweight routes — extract presentational components

Some routes (`NewRun.tsx` is the canonical example) combine 10+
`useEffect`s with Radix Select trees that interact badly with jsdom
under vitest — render hangs are common. The reliable pattern is to
extract any small piece of UI whose behavior depends only on props
into its own component under `src/components/` and test that
directly:

```tsx
// src/components/OllamaUpstreamAlert.tsx — pure props in, JSX out
export function OllamaUpstreamAlert({ provider, health }: Props) {
  const visible = provider === "ollama" && health?.status === "down";
  if (!visible) return null;
  return <div role="alert">…</div>;
}
```

```tsx
// src/__tests__/NewRun.ollamaAlert.test.tsx — 3-second suite of 7 assertions
it("renders the alert when provider=ollama and status=down", () => {
  render(<OllamaUpstreamAlert provider="ollama" health={_h("down")} />);
  expect(screen.getByRole("alert").textContent).toMatch(/unreachable/i);
});
```

vs. the full-route test that would mock 6 hooks and still hang on
mount. If you find yourself mocking five or more hooks just to test
one piece of conditional JSX, extract.

## Playwright (e2e)

`web/frontend/playwright.config.ts` auto-boots both servers via the
`webServer` array:

1. **Backend on `:8000`** with `FAKE_LLM=1`, an on-disk SQLite file at
   `web/frontend/e2e/.pw-data/e2e.db`, and a hardcoded test bcrypt
   hash. The command is `alembic upgrade head && python -m uvicorn
   app.main:app --host 127.0.0.1 --port 8000`.
2. **Vite dev server on `:5173`** with `--host 127.0.0.1 --port 5173
   --strictPort`, proxying `/api` → `:8000`.

`e2e/globalSetup.ts` creates `.pw-data` before either server starts —
done in Node (cross-shell safe) instead of inlining `mkdir -p` or
`if not exist` in the backend command, since `child_process.spawn`
picks whichever shell happens to be on PATH.

Run with:

```bash
cd web/frontend
npx playwright test
```

Single worker (`workers: 1`, `fullyParallel: false`) because the
backend has one `GLOBAL_RUN_LOCK` and serialised runs are the v1
contract. Don't bump this.

Login uses `test-admin` / `password`. The credentials live in the
config — don't move them elsewhere, the e2e is the only thing that
needs them.

## Red-green discipline for regression tests

When you fix a bug, write the test, then **revert your fix and confirm
the test fails**. Then restore the fix and confirm the test passes.

A regression test that has only ever been seen green could be passing
for the wrong reasons. The recent resume-button fix is the canonical
example: `RunView.resume.test.tsx` asserts that clicking Resume calls
`navigate("/runs/<new-id>")`. Without the red-green check, the test
would pass even if the `navigate(...)` call were entirely missing from
`RunView.tsx` — the spy would simply never be invoked, and a less
precise assertion would let that through. Only the red phase proves
the assertion is wired to the behaviour it claims to verify.

## The "ports free" reflex

Both Playwright servers bind specific ports. If a previous run was
killed mid-test, leftover `python -m uvicorn` or `vite` processes hold
those ports and the next `npx playwright test` fails to bind.

```bash
netstat -ano | grep -E ':(8000|5173) '
taskkill //PID <pid> //F
```

Make this check reflexive before re-running the e2e suite — five
seconds here saves a confusing 120-second `webServer` timeout.

## Next reads

- [`backend-dev.md`](backend-dev.md) — extension patterns the tests exercise
- [`frontend-dev.md`](frontend-dev.md) — the hooks and components under test
- [`api.md`](api.md) — endpoint and SSE contract the integration tests pin
