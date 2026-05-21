"""In-memory + DB-backed login rate limiter.

Keyed on the client IP (taken from ``X-Forwarded-For`` first header, then
``request.client.host`` as fallback — Coolify's Traefik sets XFF).

State lives in memory for the hot path; every attempt is also persisted
to the ``login_attempts`` table so a server restart does NOT reset an
ongoing lockout. On the first ``check()`` per IP after a fresh process
start, we hydrate the in-memory bucket from the DB to honor any pending
ban window the previous process had open.

A successful login clears the bucket for that IP (the user proved
they own the account), so a typo-then-correct sequence isn't punished
beyond the first wrong attempts.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import LoginAttempt


# Defaults from the plan: 5 attempts per 5 minutes.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_WINDOW_SECONDS = 5 * 60


def client_ip(request: Request) -> str:
    """Best-effort client IP extraction.

    ``X-Forwarded-For`` may contain a comma-separated chain; the leftmost
    value is the original client per RFC 7239 convention.
    """
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        first = xff.split(",", 1)[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    # Last-resort: a stable but harmless string so the limiter still
    # functions in pathological cases (e.g. ASGI test client without
    # a `client` attribute).
    return "unknown"


@dataclass
class _Bucket:
    """Per-IP rolling window of failed-attempt timestamps."""

    timestamps: Deque[datetime] = field(default_factory=deque)
    hydrated: bool = False


class LoginRateLimiter:
    """Token-bucket-style limiter over a sliding time window.

    A bucket records timestamps of *failed* login attempts within the
    window. A successful login resets the bucket. ``check()`` raises
    HTTP 401 (matching the plan's response code) with a ``Retry-After``
    header when the bucket is full.
    """

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._buckets: Dict[str, _Bucket] = {}
        # Lazy init so the lock binds to whichever loop is actually
        # running when first awaited (same reason as event_bus). The
        # singleton is constructed at module import time, before uvicorn
        # has started its loop, so eager creation would bind to a
        # short-lived loop and trip ``RuntimeError`` later.
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ----- public API ------------------------------------------------- #

    def reset(self) -> None:
        """Drop all in-memory state. Used by tests and the restart sim."""
        self._buckets.clear()
        # Drop the cached lock too so the next ``_get_lock()`` rebinds
        # to whichever loop the next test owns.
        self._lock = None

    async def check(self, request: Request, db: AsyncSession) -> None:
        """Raise 401 if the caller is currently locked out.

        On the FIRST call per IP, the in-memory bucket is hydrated from
        ``login_attempts`` so a process restart cannot reset the lockout.
        """
        ip = client_ip(request)
        async with self._get_lock():
            bucket = self._buckets.get(ip)
            if bucket is None:
                bucket = _Bucket()
                self._buckets[ip] = bucket
            if not bucket.hydrated:
                await self._hydrate(bucket, ip, db)
            self._prune(bucket)
            if len(bucket.timestamps) >= self.max_attempts:
                retry_after = self._seconds_until_free(bucket)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Too many login attempts",
                    headers={"Retry-After": str(retry_after)},
                )

    async def record(
        self, request: Request, db: AsyncSession, succeeded: bool
    ) -> None:
        """Persist an attempt to the DB and update the in-memory bucket.

        On success, the IP's bucket is cleared (the user proved control).
        On failure, the timestamp is appended.
        """
        ip = client_ip(request)
        now = datetime.now(timezone.utc)
        db.add(LoginAttempt(ip=ip, succeeded=succeeded, attempted_at=now))
        await db.commit()
        async with self._get_lock():
            bucket = self._buckets.setdefault(ip, _Bucket(hydrated=True))
            if succeeded:
                bucket.timestamps.clear()
            else:
                bucket.timestamps.append(now)
            bucket.hydrated = True

    # ----- internals -------------------------------------------------- #

    async def _hydrate(self, bucket: _Bucket, ip: str, db: AsyncSession) -> None:
        """Load recent FAILED attempts for ``ip`` from the DB into the bucket.

        Only loads the failures within the current window. A successful
        attempt in the recent past does NOT clear historical failures here
        because we'd then have to model "last_success_at"; the simpler
        and equally correct rule is: only failures within the window
        count. Once a success is recorded via ``record(..., True)``, the
        bucket is cleared in-memory and future hydrations will only pick
        up failures *after* that success (because the window has moved).
        """
        bucket.hydrated = True
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
        stmt = (
            select(LoginAttempt.attempted_at, LoginAttempt.succeeded)
            .where(LoginAttempt.ip == ip)
            .where(LoginAttempt.attempted_at >= cutoff)
            .order_by(LoginAttempt.attempted_at.asc())
        )
        result = await db.execute(stmt)
        rows = result.all()
        # Walk forward; clear the running list on any success (mirrors the
        # in-memory rule), append on each failure.
        running: Deque[datetime] = deque()
        for attempted_at, succeeded in rows:
            ts = self._coerce_aware(attempted_at)
            if succeeded:
                running.clear()
            else:
                running.append(ts)
        bucket.timestamps = running

    def _prune(self, bucket: _Bucket) -> None:
        """Drop timestamps that fell out of the rolling window."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.window_seconds)
        while bucket.timestamps and bucket.timestamps[0] < cutoff:
            bucket.timestamps.popleft()

    def _seconds_until_free(self, bucket: _Bucket) -> int:
        """How many seconds until the oldest in-window failure expires."""
        if not bucket.timestamps:
            return 0
        oldest = bucket.timestamps[0]
        free_at = oldest + timedelta(seconds=self.window_seconds)
        delta = (free_at - datetime.now(timezone.utc)).total_seconds()
        # Always at least 1 so clients don't spin retrying.
        return max(1, int(delta) + 1)

    @staticmethod
    def _coerce_aware(dt: datetime) -> datetime:
        """SQLite returns naive datetimes; treat them as UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt


# Process-wide singleton — routers import this directly so the bucket
# state is shared across all requests.
login_rate_limiter = LoginRateLimiter()


__all__ = [
    "LoginRateLimiter",
    "login_rate_limiter",
    "client_ip",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_WINDOW_SECONDS",
]
