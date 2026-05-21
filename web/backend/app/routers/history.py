"""``GET /api/history`` — cursor-paginated run summaries.

Uses **keyset pagination** on ``(created_at DESC, id DESC)`` rather than
``LIMIT/OFFSET``. Keyset is stable under concurrent inserts: a row added
after the first page was fetched cannot shift the second page, because
the cursor encodes the exact ``(created_at, id)`` of the last row
returned, and the next page selects only rows strictly less than that
key. Offset pagination would skip or duplicate rows in the same scenario.

Cursor wire format:
    ``base64url("{created_at_iso}|{run_id}")``

The ISO timestamp keeps the cursor self-describing for debugging; the
``|`` separator is illegal in both UUID4 and ISO-8601 forms, so the
split is unambiguous. Decoding errors raise HTTP 400 — never 500 — so
a malformed querystring is a client error, not a server bug.

Tuple comparison: SQLAlchemy emits ``(created_at, id) < (?, ?)`` which
SQLite implements as a lexicographic comparison (the dialect supports
row-value comparisons since 3.15). On Postgres this maps to the native
row-value syntax that the (created_at DESC, id DESC) index can serve as
an index seek.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Run
from ..schemas import AuthUser, HistoryPage, RunSummary
from . import register

# --------------------------------------------------------------------------- #
# Auth dependency — soft import                                               #
# --------------------------------------------------------------------------- #
# Per the plan, history is JWT-protected in production. We import the
# real dependency when available so production deployments enforce auth;
# tests that don't yet provide a token can override it via
# ``app.dependency_overrides``.

try:
    from ..auth import get_current_user
except ImportError:  # pragma: no cover — auth module is part of the foundation
    def get_current_user() -> AuthUser:  # type: ignore[no-redef]
        return AuthUser(username="anonymous")


router = APIRouter(prefix="/history", tags=["history"])


# --------------------------------------------------------------------------- #
# Cursor helpers                                                              #
# --------------------------------------------------------------------------- #

_CURSOR_SEP = "|"


def _encode_cursor(created_at: datetime, run_id: object) -> str:
    """Pack ``(created_at, run_id)`` into a base64-url string."""
    raw = f"{created_at.isoformat()}{_CURSOR_SEP}{run_id}".encode("utf-8")
    # urlsafe_b64encode produces an ASCII bytes object; strip padding so
    # the cursor is friendly in querystrings (we re-pad on decode).
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Unpack a cursor string. Raise HTTP 400 on any malformed input.

    Returns ``(created_at, run_id_str)``. We keep the run_id as a string
    so the comparison maps cleanly onto whatever the column type is
    (``String(36)`` on SQLite, native UUID on Postgres — both compare
    correctly against a string literal).
    """
    try:
        # Re-pad to a length divisible by 4 — urlsafe_b64encode strips '='.
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + padding)
        text = raw.decode("utf-8")
        ts_str, _, run_id = text.partition(_CURSOR_SEP)
        if not ts_str or not run_id:
            raise ValueError("cursor missing separator")
        created_at = datetime.fromisoformat(ts_str)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid cursor: {exc}",
        ) from exc
    return created_at, run_id


# --------------------------------------------------------------------------- #
# Row → schema projection                                                     #
# --------------------------------------------------------------------------- #


def _to_summary(row: Run) -> RunSummary:
    """Project a ``Run`` ORM row into the ``RunSummary`` Pydantic model.

    Computes ``elapsed_seconds`` from ``started_at``/``finished_at`` when
    both are present; otherwise leaves it ``None`` (in-flight or queued).
    """
    elapsed: Optional[float] = None
    if row.started_at is not None and row.finished_at is not None:
        elapsed = (row.finished_at - row.started_at).total_seconds()
    return RunSummary(
        id=row.id,
        ticker=row.ticker,
        asset_type=row.asset_type,  # type: ignore[arg-type]
        analysis_date=row.analysis_date,
        status=row.status,  # type: ignore[arg-type]
        rating=row.rating,  # type: ignore[arg-type]
        llm_provider=row.llm_provider,
        research_depth=row.research_depth,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        elapsed_seconds=elapsed,
    )


# --------------------------------------------------------------------------- #
# Route                                                                       #
# --------------------------------------------------------------------------- #


@router.get("", response_model=HistoryPage)
@router.get("/", response_model=HistoryPage)
async def list_history(
    cursor: Optional[str] = Query(None, description="Opaque keyset cursor."),
    limit: int = Query(20, ge=1, le=100, description="Page size (cap 100)."),
    ticker: Optional[str] = Query(None, description="Exact-match ticker filter."),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Exact-match status filter (e.g. 'completed').",
    ),
    session: AsyncSession = Depends(get_session),
    _user: AuthUser = Depends(get_current_user),
) -> HistoryPage:
    """List runs newest-first with cursor pagination.

    Fetches ``limit + 1`` rows so we can detect a follow-on page without
    a second COUNT query; if the +1th row exists we encode it as the
    ``next_cursor`` and drop it from the response body.
    """
    stmt = select(Run).order_by(desc(Run.created_at), desc(Run.id))
    if ticker is not None:
        stmt = stmt.where(Run.ticker == ticker)
    if status_filter is not None:
        stmt = stmt.where(Run.status == status_filter)

    if cursor is not None:
        created_at, run_id = _decode_cursor(cursor)
        # Keyset condition: strictly past the last row we returned.
        # Tuple comparison works on both SQLite (row-value, lexicographic)
        # and Postgres (native row-value, index-friendly).
        stmt = stmt.where(
            tuple_(Run.created_at, Run.id) < tuple_(created_at, run_id)
        )

    stmt = stmt.limit(limit + 1)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    next_cursor: Optional[str] = None
    if len(rows) > limit:
        # The (limit+1)th row tells us "there's more" — its keyset becomes
        # the cursor; we drop it from the visible page so the page has
        # exactly `limit` items.
        rows = list(rows[:limit])
        last = rows[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    else:
        rows = list(rows)

    return HistoryPage(
        items=[_to_summary(r) for r in rows],
        next_cursor=next_cursor,
    )


register(router)


__all__ = ["router"]
