"""Run ownership and admin-default helpers."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Run, UserDefaults as UserDefaultsModel
from ..schemas import AuthUser, RunRequest, UserDefaults


def user_can_access_run(user: AuthUser, row: Run) -> bool:
    """Return True if the user may view or mutate the run."""
    if user.role == "admin":
        return True
    return str(row.user_id) == str(user.id)


def require_run_access(user: AuthUser, row: Run | None) -> Run:
    """Return the run row or raise 404 if missing or inaccessible."""
    if row is None or not user_can_access_run(user, row):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found",
        )
    return row


def _row_to_defaults(row: UserDefaultsModel | None) -> UserDefaults:
    if row is None:
        return UserDefaults()
    return UserDefaults(
        llm_provider=row.llm_provider,
        quick_think_llm=row.quick_think_llm,
        deep_think_llm=row.deep_think_llm,
        research_depth=row.research_depth,  # type: ignore[arg-type]
        analysts=list(row.analysts) if row.analysts else None,  # type: ignore[arg-type]
        output_language=row.output_language,
        thinking_config=row.thinking_config,  # type: ignore[arg-type]
        enable_checkpoint=row.enable_checkpoint,
    )


async def load_admin_defaults(db: AsyncSession) -> UserDefaults:
    """Load the singleton admin defaults row."""
    row = await db.get(UserDefaultsModel, 1)
    return _row_to_defaults(row)


async def apply_admin_defaults_for_user(
    body: RunRequest,
    db: AsyncSession,
) -> RunRequest:
    """Override LLM-related fields from admin defaults for regular users."""
    defaults = await load_admin_defaults(db)
    provider = defaults.llm_provider or "ollama"
    quick = defaults.quick_think_llm or "glm-5.2"
    deep = defaults.deep_think_llm or "glm-5.2"
    language = defaults.output_language or "English"
    checkpoint = (
        defaults.enable_checkpoint if defaults.enable_checkpoint is not None else True
    )
    thinking = defaults.thinking_config
    return body.model_copy(
        update={
            "llm_provider": provider,
            "quick_think_llm": quick,
            "deep_think_llm": deep,
            "output_language": language,
            "enable_checkpoint": checkpoint,
            "google_thinking_level": thinking.google_thinking_level if thinking else None,
            "openai_reasoning_effort": thinking.openai_reasoning_effort if thinking else None,
            "anthropic_effort": thinking.anthropic_effort if thinking else None,
        }
    )


__all__ = [
    "apply_admin_defaults_for_user",
    "load_admin_defaults",
    "require_run_access",
    "user_can_access_run",
]
