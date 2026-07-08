#!/usr/bin/env python3
"""One-shot Postgres password rotation for Coolify deploys.

Usage (inside the app container, with current DATABASE_URL still valid):

    ROTATE_DB_PASSWORD='new-secret' python scripts/rotate_db_password.py

Then update Coolify ``DATABASE_URL`` to use the same password and redeploy.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def _main() -> None:
    new_password = os.environ.get("ROTATE_DB_PASSWORD")
    if not new_password:
        print("ROTATE_DB_PASSWORD is required", file=sys.stderr)
        sys.exit(1)

    dsn = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "ALTER USER tradingagents WITH PASSWORD $1",
            new_password,
        )
    finally:
        await conn.close()

    print("Password rotated for tradingagents")


if __name__ == "__main__":
    asyncio.run(_main())
