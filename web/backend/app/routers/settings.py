"""Settings router: API-key storage + user defaults.

Endpoints (all require JWT auth):

- ``GET    /api/settings/api-keys``        → list[ApiKeyStatus]
- ``PUT    /api/settings/api-keys/{env}``  → 204, Fernet-encrypts and UPSERTs
- ``DELETE /api/settings/api-keys/{env}``  → 204
- ``GET    /api/settings/defaults``        → UserDefaults
- ``PUT    /api/settings/defaults``        → UserDefaults (partial merge into singleton)

Safety properties (asserted by tests):

- API-key endpoints NEVER return plaintext key values in any response.
- ``{env}`` is validated against ``PROVIDER_API_KEY_ENV`` (the canonical
  provider→env-var mapping) so we can't end up with arbitrary rows in
  ``api_keys`` from a typo or a malicious client.
- Defaults uses a true partial merge (``exclude_unset=True``) so a PUT
  with only one field doesn't clobber siblings.
- ``GET /defaults`` auto-heals stale model names: if the saved
  quick/deep model is no longer in the live catalog for the saved
  provider, the response returns ``null`` for that field (the DB row is
  left untouched — the next PUT overwrites cleanly).
- ``PUT /defaults`` validates provider+model against the live catalog
  and returns 400 with the available list if mismatched. ``null`` is
  accepted (that's how auto-heal stores its "I don't know" state).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog as catalog_svc
from app import crypto
from app.db import get_session
from app.models import ApiKey as ApiKeyModel
from app.models import UserDefaults as UserDefaultsModel
from app.schemas import ApiKeyStatus, UserDefaults
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

from . import register

log = logging.getLogger(__name__)


# --- Auth dependency (soft dep on the AUTH team) --------------------------- #
# Once ``app/auth.py`` lands, the try-block import replaces the stub
# automatically — no edits needed here. Mirrors the catalog router pattern.
try:
    from app.auth import get_current_user  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — covered when AUTH team's module is absent
    from app.schemas import AuthUser

    def get_current_user() -> AuthUser:
        """Placeholder dep until the AUTH team's ``get_current_user`` lands."""
        return AuthUser(username="anonymous")


# Set of provider env-var names this system knows about. ``None`` values
# (e.g. ollama, which has no key) are filtered out — there's no key to
# store for them, so they don't appear in the api-keys UI at all.
_KNOWN_ENVS: frozenset[str] = frozenset(
    env for env in PROVIDER_API_KEY_ENV.values() if env
)


router = APIRouter(prefix="/settings", tags=["settings"])


# --------------------------------------------------------------------------- #
# Request models                                                              #
# --------------------------------------------------------------------------- #


class _ApiKeyPutBody(BaseModel):
    """Body for PUT /api-keys/{env} — a single ``value`` field."""

    value: str


# --------------------------------------------------------------------------- #
# Validation helper                                                           #
# --------------------------------------------------------------------------- #


def _ensure_known_env(env: str) -> None:
    """Reject env names not in the canonical provider→env mapping.

    Returning 400 (not 404) because the client supplied bad input — the
    set of accepted envs is part of the API contract and not a resource
    the client looks up.
    """
    if env not in _KNOWN_ENVS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider env-var: {env!r}",
        )


# --------------------------------------------------------------------------- #
# /api-keys                                                                   #
# --------------------------------------------------------------------------- #


@router.get("/api-keys", response_model=list[ApiKeyStatus])
async def list_api_keys(
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> list[ApiKeyStatus]:
    """Return one ``ApiKeyStatus`` per provider env-var the system knows about.

    NEVER returns the plaintext value — only ``configured`` (does a row
    exist?) and ``last_updated`` (when was it last upserted?).
    """
    result = await db.execute(select(ApiKeyModel))
    stored = {row.provider_env: row for row in result.scalars().all()}

    # Sort for stable output — alphabetical by env name reads well in the UI
    # and keeps test assertions deterministic without a sort step.
    return [
        ApiKeyStatus(
            provider_env=env,
            configured=env in stored,
            last_updated=stored[env].updated_at if env in stored else None,
        )
        for env in sorted(_KNOWN_ENVS)
    ]


@router.put(
    "/api-keys/{env}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def put_api_key(
    env: str,
    body: _ApiKeyPutBody,
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> Response:
    """Encrypt ``value`` and UPSERT into ``api_keys`` keyed by env-var name.

    Returns 204 with an empty body — we never echo the plaintext back.
    """
    _ensure_known_env(env)

    ciphertext = crypto.encrypt(body.value)

    existing = await db.get(ApiKeyModel, env)
    if existing is None:
        row = ApiKeyModel(provider_env=env, encrypted_value=ciphertext)
        db.add(row)
    else:
        existing.encrypted_value = ciphertext
        # SQLAlchemy's ``onupdate=func.now()`` triggers on column changes
        # other than the primary key; we touch the column explicitly so a
        # PUT with the same plaintext still bumps ``updated_at``. Falling
        # back to a tz-aware UTC stamp keeps SQLite (which has no ``now()``)
        # honest in tests.
        existing.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api-keys/{env}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_api_key(
    env: str,
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> Response:
    """Drop the stored row for ``env``. No-op if it never existed."""
    _ensure_known_env(env)

    await db.execute(sa_delete(ApiKeyModel).where(ApiKeyModel.provider_env == env))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# /defaults                                                                   #
# --------------------------------------------------------------------------- #


def _row_to_defaults(row: UserDefaultsModel | None) -> UserDefaults:
    """Project the ORM row (or None) into the Pydantic ``UserDefaults`` shape.

    If ``row`` is None, returns the Pydantic defaults (``enable_checkpoint=True``,
    ``research_depth=1``, ``analysts=["market","social"]``,
    ``llm_provider="ollama"``, ``quick_think_llm`` / ``deep_think_llm`` =
    ``"glm-5.2"``). The ``ThinkingConfig`` sub-object is stored as a JSON blob — Pydantic re-validates it on construction, so a malformed
    blob would surface here as a 500. That's fine: it would mean the DB
    was hand-edited to an invalid state and the operator should know.
    """
    if row is None:
        return UserDefaults()
    return UserDefaults(
        llm_provider=row.llm_provider,
        quick_think_llm=row.quick_think_llm,
        deep_think_llm=row.deep_think_llm,
        research_depth=row.research_depth,  # type: ignore[arg-type]
        analysts=row.analysts,  # type: ignore[arg-type]
        output_language=row.output_language,
        thinking_config=row.thinking_config,  # type: ignore[arg-type]
        enable_checkpoint=row.enable_checkpoint,
        updated_at=row.updated_at,
    )


async def _live_model_ids(provider: str, mode: str) -> set[str]:
    """Return the set of valid model IDs for (provider, mode) per the live catalog.

    The catalog returns ``CatalogModel`` entries; we project out ``id``.
    The synthetic ``__custom__`` entry (for providers that accept any
    typed model ID) is intentionally treated as "anything goes" by the
    caller — see ``_is_model_valid``.
    """
    models = await catalog_svc.list_models(provider, mode)  # type: ignore[arg-type]
    return {m.id for m in models}


async def _is_model_valid(provider: str | None, model: str | None, mode: str) -> bool:
    """True iff ``model`` is a valid choice for ``provider`` per the live catalog.

    Returns True for ``None`` (clearing is always allowed) and for ``None``
    provider (we can't validate without a provider — let it through and
    rely on the runs router's defense-in-depth check before engine launch).

    Providers whose catalog still includes the ``__custom__`` sentinel
    accept any model ID — that's the "Custom model ID" affordance for
    openrouter / azure / deepseek-style providers.
    """
    if model is None or provider is None:
        return True
    ids = await _live_model_ids(provider, mode)
    if "__custom__" in ids:
        return True
    return model in ids


@router.get("/defaults", response_model=UserDefaults)
async def get_defaults(
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> UserDefaults:
    """Return the singleton ``user_defaults`` row with stale models auto-healed.

    If the saved ``quick_think_llm`` / ``deep_think_llm`` is no longer in
    the live catalog for the saved ``llm_provider``, that field is
    returned as ``None``. The DB row is NOT mutated — the next PUT
    overwrites cleanly. Without ``llm_provider`` we can't validate, so
    the saved values are returned as-is.
    """
    row = await db.get(UserDefaultsModel, 1)
    defaults = _row_to_defaults(row)

    provider = defaults.llm_provider
    if provider is None:
        return defaults

    if defaults.quick_think_llm is not None and not await _is_model_valid(
        provider, defaults.quick_think_llm, "quick"
    ):
        log.info(
            "settings.defaults.autoheal_quick_stale",
            extra={"provider": provider, "stale_model": defaults.quick_think_llm},
        )
        defaults = defaults.model_copy(update={"quick_think_llm": None})

    if defaults.deep_think_llm is not None and not await _is_model_valid(
        provider, defaults.deep_think_llm, "deep"
    ):
        log.info(
            "settings.defaults.autoheal_deep_stale",
            extra={"provider": provider, "stale_model": defaults.deep_think_llm},
        )
        defaults = defaults.model_copy(update={"deep_think_llm": None})

    return defaults


@router.put("/defaults", response_model=UserDefaults)
async def put_defaults(
    body: UserDefaults,
    db: AsyncSession = Depends(get_session),
    _user=Depends(get_current_user),
) -> UserDefaults:
    """Partial-merge ``body`` into the singleton row, creating it if missing.

    Uses ``model_dump(exclude_unset=True)`` so fields the client did NOT
    send are left untouched on the row — a PUT with just ``llm_provider``
    must not wipe ``quick_think_llm`` to None.

    **Model validation:** any non-null quick/deep model in the request
    must be in the live catalog for whichever provider ends up effective
    (sent provider, else current row provider). Mismatches return 400
    with the available models in ``detail``. ``null`` clears are always
    allowed (auto-heal uses this).
    """
    # Use model_fields_set so explicit "I sent enable_checkpoint=False" is
    # honored but a Pydantic-applied default is not treated as user intent.
    sent = body.model_dump(exclude_unset=True)

    # --- Validate quick/deep models against the live catalog --------------- #
    existing_row = await db.get(UserDefaultsModel, 1)
    effective_provider: str | None = (
        sent.get("llm_provider")
        if "llm_provider" in sent
        else (existing_row.llm_provider if existing_row is not None else None)
    )

    for field, mode in (("quick_think_llm", "quick"), ("deep_think_llm", "deep")):
        if field not in sent:
            continue
        value = sent[field]
        if value is None:
            continue  # Clearing is always OK.
        if effective_provider is None:
            # No provider to validate against — refuse rather than save a
            # value with no semantic anchor.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cannot save {field}={value!r} without an llm_provider. "
                    "Include llm_provider in the PUT body."
                ),
            )
        if not await _is_model_valid(effective_provider, value, mode):
            ids = sorted(await _live_model_ids(effective_provider, mode))
            available = ", ".join(ids) if ids else "(no models available)"
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Model {value!r} is not available on provider "
                    f"{effective_provider!r}. Available: {available}"
                ),
            )

    row = existing_row
    if row is None:
        # Build the row from defaults + sent overrides. The Pydantic
        # ``UserDefaults()`` baseline gives us ``enable_checkpoint=True``
        # plus None for the optional fields, which matches the schema.
        baseline = UserDefaults().model_dump()
        baseline.update(sent)
        row = UserDefaultsModel(
            id=1,
            llm_provider=baseline.get("llm_provider"),
            quick_think_llm=baseline.get("quick_think_llm"),
            deep_think_llm=baseline.get("deep_think_llm"),
            research_depth=baseline.get("research_depth"),
            analysts=baseline.get("analysts"),
            output_language=baseline.get("output_language"),
            thinking_config=baseline.get("thinking_config"),
            enable_checkpoint=baseline.get("enable_checkpoint", True),
        )
        db.add(row)
    else:
        # Merge: only assign for fields the client actually sent.
        for field, value in sent.items():
            if field == "updated_at":
                # Server-managed; ignore client-supplied value.
                continue
            if hasattr(row, field):
                setattr(row, field, value)
        # Touch updated_at so SQLite (no server-side ``onupdate``) reflects
        # the mutation even when ``sent`` is empty.
        row.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(row)
    return _row_to_defaults(row)


register(router)


__all__ = ["router", "get_current_user"]
