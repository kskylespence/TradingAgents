"""Application settings loaded from environment variables.

See the plan's "Coolify deployment → Env vars" table for the canonical list.
The `Settings` model exposes typed access and a cached `get_settings()`
dependency for use in FastAPI routers.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the TradingAgents web backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_title: str = "TradingAgents Web UI"
    app_env: str = Field(default="development")
    debug: bool = Field(default=False)

    # --- Auth (admin/single-user) ---
    admin_username: str = Field(default="admin")
    admin_password_hash: str = Field(
        ...,
        min_length=60,
        description=(
            "Required. bcrypt hash (always 60 chars), produced by "
            "passlib.hash.bcrypt. MUST be set via env var."
        ),
    )
    jwt_secret: str = Field(
        ...,
        min_length=32,
        description="Required HMAC secret for JWT signing. MUST be set via env var.",
    )
    jwt_ttl_seconds: int = Field(default=604800)  # 7 days

    # --- Encryption (Fernet, for stored API keys) ---
    fernet_key: str = Field(
        ...,
        min_length=44,
        description=(
            "Required Fernet master key (44-char urlsafe-b64). MUST be set via "
            "env var. Generate with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        ),
    )

    # --- Database ---
    database_url: str = Field(
        default="sqlite+aiosqlite:///:memory:",
        description=(
            "SQLAlchemy URL. Production: postgresql+asyncpg://...neon.tech/...?ssl=require"
        ),
    )

    # --- Disk ---
    data_dir: Path = Field(default=Path("/data/tradingagents"))
    retention_days: int = Field(default=90)

    # --- Coolify magic vars (used for CSP construction in later tasks) ---
    coolify_fqdn: Optional[str] = None
    coolify_url: Optional[str] = None

    # --- Static assets (the React build) ---
    static_dir: Path = Field(
        default=Path(__file__).resolve().parent / "static",
        description="Directory served as catch-all (the React build output).",
    )

    @field_validator("data_dir", "static_dir", mode="before")
    @classmethod
    def _coerce_path(cls, v: object, info: ValidationInfo) -> Path:
        if isinstance(v, Path):
            return v
        return Path(str(v))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor, suitable as a FastAPI dependency."""
    return Settings()
