"""Users router: admin-only account management.

Endpoints (all require an **admin** JWT):

- ``GET    /api/users``            → list[UserSummary]
- ``POST   /api/users``            → 201 UserSummary
- ``DELETE /api/users/{user_id}``  → 204

Replaces the previous "add a user by editing Python" workflow, where a new
account meant adding a hardcoded seeder like ``ensure_rob_user`` plus an env
var plus a redeploy.

Safety properties (asserted by ``tests/test_users_admin.py``):

- No response ever contains ``password`` or ``password_hash``. ``UserSummary``
  has no such field, so this holds by construction rather than by care.
- ``role`` is hardcoded to ``"user"`` on create. ``CreateUserRequest`` has no
  ``role`` field and Pydantic ignores unknown keys, so a client POSTing
  ``role: "admin"`` is silently dropped — there is no escalation path.
- Username collisions are rejected **case-insensitively**. The DB's UNIQUE
  constraint is case-sensitive, so without this both ``Rob`` and ``rob`` can
  exist as two accounts that look identical in the UI.
- Deleting a user who owns runs is refused (409) rather than cascading. Runs
  are the product of the app; destroying analysis history as a side effect of
  removing a login would be surprising and unrecoverable.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, require_admin
from app.db import get_session
from app.models import BOOTSTRAP_ADMIN_ID, Run, User
from app.schemas import AuthUser, CreateUserRequest, UserSummary

from . import register

log = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _to_summary(user: User, run_count: int) -> UserSummary:
    """Project an ORM row onto the wire schema (drops ``password_hash``)."""
    return UserSummary(
        id=user.id,
        username=user.username,
        role=user.role,  # type: ignore[arg-type]
        created_at=user.created_at,
        run_count=run_count,
    )


async def _count_runs(db: AsyncSession, user_id: UUID | str) -> int:
    """Number of runs owned by ``user_id``."""
    result = await db.execute(
        select(func.count(Run.id)).where(Run.user_id == str(user_id))
    )
    return int(result.scalar_one() or 0)


# --------------------------------------------------------------------------- #
# GET /users                                                                  #
# --------------------------------------------------------------------------- #


@router.get("", response_model=list[UserSummary])
async def list_users(
    db: AsyncSession = Depends(get_session),
    _admin: AuthUser = Depends(require_admin),
) -> list[UserSummary]:
    """Every account, oldest first, each with its run count.

    Run counts come from a grouped subquery LEFT-joined onto users, so a
    user with no runs still appears (with 0) instead of being dropped by
    an inner join — and we issue one query rather than one per user.
    """
    run_counts = (
        select(Run.user_id.label("user_id"), func.count(Run.id).label("run_count"))
        .group_by(Run.user_id)
        .subquery()
    )

    result = await db.execute(
        select(User, func.coalesce(run_counts.c.run_count, 0))
        .outerjoin(run_counts, run_counts.c.user_id == User.id)
        .order_by(User.created_at, User.username)
    )

    return [_to_summary(user, int(count or 0)) for user, count in result.all()]


# --------------------------------------------------------------------------- #
# POST /users                                                                 #
# --------------------------------------------------------------------------- #


@router.post("", response_model=UserSummary, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    db: AsyncSession = Depends(get_session),
    _admin: AuthUser = Depends(require_admin),
) -> UserSummary:
    """Create a plain (non-admin) account.

    ``body.username`` arrives already stripped and length-checked by
    ``CreateUserRequest``; ``body.password`` is already known to fit
    bcrypt's 72-byte window.
    """
    username = body.username

    existing = await db.execute(
        select(User.id).where(func.lower(User.username) == username.lower())
    )
    if existing.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user named {username!r} already exists.",
        )

    user = User(
        # Generate the id here rather than leaning on the column default.
        # `UuidType` is String(36) off Postgres, and aiosqlite refuses to bind
        # a raw `uuid.UUID` to it. A string round-trips on both engines —
        # Postgres' UUID(as_uuid=True) coerces it. Same approach as
        # `services/users.py:upsert_admin_user`.
        id=str(uuid4()),
        username=username,
        password_hash=hash_password(body.password),
        role="user",  # hardcoded — never taken from the request body
    )
    db.add(user)

    try:
        await db.commit()
    except IntegrityError:
        # Catches an *exact-case* duplicate inserted between the check above
        # and this commit.
        #
        # It does NOT close the case-insensitive window, despite the check
        # above being case-insensitive: `users.username` carries a plain
        # case-sensitive UNIQUE, so two concurrent creates of "Rob" and
        # "rob" both pass the pre-check AND both commit without violating
        # it. Closing that properly needs a functional unique index on
        # `lower(username)` — a migration, deliberately out of scope here.
        # Exposure is two admins creating case-variant names simultaneously;
        # the failure mode is cosmetic (two accounts that look alike in the
        # list), not an auth bypass — login matches usernames exactly, so a
        # case-variant is a separate account, never a way into an existing
        # one.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user named {username!r} already exists.",
        ) from None

    await db.refresh(user)
    log.info("users.created", extra={"username": username})
    # Freshly created, so it cannot own runs yet.
    return _to_summary(user, 0)


# --------------------------------------------------------------------------- #
# DELETE /users/{user_id}                                                     #
# --------------------------------------------------------------------------- #


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_session),
    admin: AuthUser = Depends(require_admin),
) -> Response:
    """Remove an account. Refuses in four cases, each with its own status.

    Note the JWT is stateless: deleting a user does not invalidate a session
    they already hold. See the revocation note in ``web/docs/api.md``.
    """
    if user_id == BOOTSTRAP_ADMIN_ID:
        # `upsert_admin_user()` re-creates this row from env on every boot,
        # so "deleting" it would silently reappear on the next restart.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The bootstrap admin is managed by environment variables and "
                "cannot be deleted here."
            ),
        )

    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account.",
        )

    user = await db.get(User, str(user_id))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    # `Run.user_id` is ondelete="RESTRICT", so Postgres would raise — but
    # SQLite only enforces foreign keys when `PRAGMA foreign_keys=ON` is set
    # per connection, and the test suite runs in-memory SQLite. Checking here
    # keeps the behaviour identical on both engines, and lets us return the
    # run count so the UI can explain the refusal.
    run_count = await _count_runs(db, user_id)
    if run_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{user.username!r} owns {run_count} run(s). Delete or reassign "
                "them before removing the account."
            ),
        )

    await db.delete(user)
    try:
        await db.commit()
    except IntegrityError:
        # Backstop: a run created between the count and the commit.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This user owns runs and cannot be deleted.",
        ) from None

    log.info("users.deleted", extra={"username": user.username})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


register(router)


__all__ = ["router"]
