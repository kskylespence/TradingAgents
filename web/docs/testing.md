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

`-p no:postgresql` disables the `pytest-postgresql` plugin by default;
on Windows it tries to import psycopg's libpq at collection time and
fails without a system libpq install. Tests that need real Postgres
re-enable it via their own conftest.

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
from app.auth import get_current_user
from app.schemas import AuthUser

def _override_user() -> AuthUser:
    return AuthUser(username="test")

app.dependency_overrides[get_current_user] = _override_user
```

Pattern lifted from `tests/test_runs_smoke.py` and `tests/test_history.py`.
Always pop the override in a `finally:` so it doesn't leak to other
tests.

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
