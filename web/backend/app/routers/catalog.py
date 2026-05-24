"""Catalog endpoints: read-only data that drives the frontend form.

All endpoints require JWT auth via ``Depends(get_current_user)``. Until
the AUTH team lands ``app/auth.py``, a temporary stub is used so this
module stays importable in isolation.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from app import catalog as catalog_svc
from app.schemas import (
    CatalogAnalyst,
    CatalogLanguage,
    CatalogModel,
    CatalogProvider,
)

from . import register

# --- Auth dependency (soft dep on the AUTH team) --------------------------- #
# TODO(AUTH team): once ``app/auth.py`` lands, the try-block import
# replaces the stub automatically. No edits needed here.
try:
    from app.auth import get_current_user  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — covered when AUTH team lands
    from app.schemas import AuthUser

    def get_current_user() -> AuthUser:
        """Placeholder dep until the AUTH team's ``get_current_user`` lands."""
        return AuthUser(username="anonymous")


router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/providers", response_model=list[CatalogProvider])
async def get_providers(
    _user=Depends(get_current_user),
) -> list[CatalogProvider]:
    """List every supported LLM provider with its API-key env var."""
    return catalog_svc.list_providers()


@router.get(
    "/models",
    response_model=list[CatalogModel],
    response_model_exclude_none=True,
)
async def get_models(
    provider: str = Query(..., description="Provider key, e.g. 'openai'."),
    mode: Literal["quick", "deep"] = Query(
        ..., description="Which model tier: quick-think vs deep-think."
    ),
    _user=Depends(get_current_user),
) -> list[CatalogModel]:
    """List candidate models for a provider/mode pair.

    ``ollama`` is live-discovered from the upstream Ollama / Ollama Cloud
    instance; other providers come from the static catalog. The Ollama
    branch sets ``curated: bool`` per model from the snapshot in
    ``app.services.ollama_curated``; non-ollama responses omit the field
    entirely (via ``response_model_exclude_none``) because we have no
    equivalent quality signal there. The frontend's optional
    ``curated?: boolean`` matches both shapes.
    """
    return await catalog_svc.list_models(provider, mode)


@router.get("/analysts", response_model=list[CatalogAnalyst])
async def get_analysts(
    asset_type: Literal["stock", "crypto"] = Query(
        ..., description="Filters analysts available for this asset class."
    ),
    _user=Depends(get_current_user),
) -> list[CatalogAnalyst]:
    """List analyst options; crypto excludes Fundamentals."""
    return catalog_svc.list_analysts(asset_type)


@router.get("/languages", response_model=list[CatalogLanguage])
async def get_languages(
    _user=Depends(get_current_user),
) -> list[CatalogLanguage]:
    """List the 11 hardcoded output languages."""
    return catalog_svc.list_languages()


register(router)
