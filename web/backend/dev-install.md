# Dev install — web/backend

The backend depends on the parent `tradingagents` package via a **path
dependency**. Plain `pip` cannot resolve that name from PyPI; you must
install the parent package first.

## Quick path (plain pip)

```bash
# From the repo root:
pip install -e .                       # installs tradingagents (editable)
pip install -e web/backend[dev]        # installs the backend + dev deps
```

After both installs you can run from `web/backend/`:

```bash
pytest tests/test_foundation_smoke.py  # smoke tests
alembic upgrade head                   # apply migration to SQLite (in-memory by default)
python -c "from app.main import app; print(app.title)"
uvicorn app.main:app --reload --port 8000
```

## With uv

```bash
# uv reads [tool.uv.sources] in web/backend/pyproject.toml.
cd web/backend
uv sync --extra dev
```

The `[tool.uv.sources]` table pins `tradingagents` to `../` editable, so
`uv` resolves it without the manual two-step.

## In Docker / Coolify

The repo-root `Dockerfile` already does:

```dockerfile
COPY pyproject.toml .
COPY tradingagents ./tradingagents
COPY cli ./cli
COPY web/backend ./web/backend
RUN pip install --no-cache-dir ./web/backend
```

That `pip install ./web/backend` fails with the plain-pip approach above
unless we first install the parent. The Dockerfile needs one extra line —
either `RUN pip install --no-cache-dir -e .` before the backend install,
or `RUN pip install --no-cache-dir . ./web/backend`. The devops agent
that owns the Dockerfile should fold this in; see "Flags for downstream
agents" in the foundation-task report.

## DATABASE_URL conventions

| Use case                         | Value                                                  |
|----------------------------------|--------------------------------------------------------|
| Local dev / `pytest`             | `sqlite+aiosqlite:///:memory:` (the default)           |
| Local Postgres in docker compose | `postgresql+asyncpg://postgres:postgres@db:5432/app`   |
| Coolify Postgres (same server)   | `postgresql+asyncpg://user:pass@<db-uuid>:5432/tradingagents` |
| Neon (managed)                   | `postgresql+asyncpg://…neon.tech/…?ssl=require`        |

The SQLite default lets the foundation smoke tests run without any
external services. Postgres-specific tests (downstream tasks) opt into
`pytest-postgresql` via their own fixtures.
