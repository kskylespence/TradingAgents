# TradingAgents Web UI

A single-user, login-gated web UI that drives the existing
`TradingAgentsGraph` engine (the same one the CLI uses — no behavior
drift). Built for [Coolify](https://coolify.io/) deployment as one
Docker image.

Submit a run from a browser, watch agents complete live via SSE, see a
five-tier rating, download the report, browse history, manage your
provider API keys — without ever opening a terminal.

> **Apache 2.0**, same as the parent project. See `../LICENSE`.

## Stack

| Backend (`backend/`) | Frontend (`frontend/`) |
|---|---|
| FastAPI + Uvicorn | Vite + React 18 + TypeScript 5 |
| SQLAlchemy 2.0 async + Alembic | Tailwind 3 + shadcn/ui |
| Neon Postgres (prod) / SQLite (dev) | TanStack React Query + react-router |
| `sse-starlette` for live streaming | `EventSource` + reducer hook |
| `passlib[bcrypt]` + PyJWT for auth | Playwright + Vitest for tests |
| Fernet for at-rest API-key encryption | react-markdown + remark-gfm |

## 60-second local dev loop

No real LLM credentials required — `FAKE_LLM=1` short-circuits the
engine to a scripted 0.3-second simulator that always returns `Buy`.

```bash
# One-time setup (from repo root):
pip install -e .                          # installs the parent tradingagents package
pip install -e web/backend[dev]           # installs the backend + test deps
cd web/frontend && npm install            # frontend deps

# Boot the backend (in one terminal):
cd web/backend
export ADMIN_USERNAME=admin
export ADMIN_PASSWORD_HASH="$(python -c 'from passlib.hash import bcrypt; print(bcrypt.hash(\"password\"))')"
export JWT_SECRET=dev-secret-not-for-production
export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export DATABASE_URL=sqlite+aiosqlite:///./dev.db
export FAKE_LLM=1
alembic upgrade head
uvicorn app.main:app --reload --port 8000

# Boot the frontend (in another terminal):
cd web/frontend
npm run dev    # http://localhost:5173 (proxies /api → :8000)
```

Open <http://localhost:5173>, log in as `admin` / `password`, submit a
SPY run, watch it complete in ~0.3 seconds, click Download Report.

## Documentation map

Start here, then read whichever is relevant to what you're doing.

| Doc | Audience | When to read |
|---|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Anyone new | First — gives you the mental model of how FastAPI, the engine, the event bus, and the SPA all fit |
| [`docs/backend-dev.md`](docs/backend-dev.md) | Contributors adding backend code | Before writing your first router/middleware/lifespan hook |
| [`docs/frontend-dev.md`](docs/frontend-dev.md) | Contributors adding frontend code | Before writing your first route/component/hook |
| [`docs/api.md`](docs/api.md) | API consumers + frontend devs | When you need to know exactly what `/api/*` returns or how SSE/CSRF/auth work |
| [`docs/testing.md`](docs/testing.md) | Anyone writing code | Before you write your first test in this subtree |
| [`docs/operations.md`](docs/operations.md) | Operators + on-call | When something is wrong in prod, or to understand env vars and secrets |
| [`../DEPLOY.md`](../DEPLOY.md) | Operators deploying | Step-by-step Coolify deployment guide |
| [`backend/dev-install.md`](backend/dev-install.md) | Local-setup reference | If `pip install -e web/backend` does anything unexpected |

For Claude Code sessions: nested [`backend/CLAUDE.md`](backend/CLAUDE.md)
and [`frontend/CLAUDE.md`](frontend/CLAUDE.md) carry the dense load-
bearing-invariants-only conventions for each subtree.

## Repository layout

```
web/
├── README.md                     # this file
├── docs/                         # human-facing dev guides
│   ├── architecture.md
│   ├── backend-dev.md
│   ├── frontend-dev.md
│   ├── api.md
│   ├── testing.md
│   └── operations.md
├── backend/                      # FastAPI app + tests
│   ├── CLAUDE.md                 # dense backend conventions
│   ├── dev-install.md
│   ├── pyproject.toml
│   ├── alembic/                  # migrations
│   └── app/
│       ├── main.py               # app factory + lifespan
│       ├── config.py             # pydantic-settings
│       ├── db.py                 # async engine + session
│       ├── models.py             # ORM (5 tables)
│       ├── schemas.py            # Pydantic / TS contract
│       ├── crypto.py             # Fernet wrappers
│       ├── auth.py               # JWT + bcrypt dep
│       ├── catalog.py            # /api/catalog/* source-of-truth wrapper
│       ├── routers/              # auto-discovered (drop a file)
│       ├── middleware/           # auto-discovered (drop a file)
│       ├── lifespan_hooks/       # auto-discovered (drop a file)
│       ├── services/             # run_service, event_bus, env_inject, ...
│       └── observers/            # WebRunObserver
└── frontend/                     # Vite + React SPA
    ├── CLAUDE.md                 # dense frontend conventions
    ├── package.json
    ├── playwright.config.ts
    ├── vite.config.ts
    ├── e2e/                      # Playwright tests
    └── src/
        ├── App.tsx               # routes wired here (the one shared file)
        ├── routes/               # one file per page
        ├── components/           # incl. shadcn/ui primitives
        ├── hooks/                # useRun, useCatalog, useHistory, ...
        ├── lib/                  # api.ts, sse.ts, types.ts (backend contract)
        └── __tests__/            # vitest
```

## Where the parent project lives

- Parent repo: [`../README.md`](../README.md)
- LangGraph pipeline + agents: `../tradingagents/`
- CLI (Typer + Rich + Questionary): `../cli/`
- Both the CLI and this Web UI call into the same
  `TradingAgentsGraph(...).propagate(ticker, date)` entry point. The
  CLI is the regression oracle when you change anything in the engine.
