"""Application settings loaded from environment variables.

See the plan's "Coolify deployment → Env vars" table for the canonical list.
The `Settings` model exposes typed access and a cached `get_settings()`
dependency for use in FastAPI routers.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator, model_validator
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
        default="",
        description=(
            "bcrypt hash (always 60 chars), produced by passlib.hash.bcrypt. "
            "Provide via ADMIN_PASSWORD_HASH OR ADMIN_PASSWORD_HASH_B64 — "
            "the post-validator promotes the b64 fallback if the direct "
            "field is empty. min_length is checked in the post-validator "
            "so an empty value falls through to the b64 fallback without "
            "tripping field-level validation."
        ),
    )
    admin_password_hash_b64: str = Field(
        default="",
        description=(
            "Optional base64-encoded bcrypt hash. Use this when the "
            "deployment platform interpolates `$` characters in env vars "
            "(Coolify mangles `$2b$12$...` regardless of its is_literal "
            "flag, dropping the `$<varname>` segment to empty and leaving "
            "a truncated hash). Base64-encoded bytes contain none of the "
            "shell metacharacters that trip platform interpolation, so "
            "this round-trips intact. Decoded into admin_password_hash "
            "by the model post-validator below."
        ),
    )
    jwt_secret: str = Field(
        ...,
        min_length=32,
        description="Required HMAC secret for JWT signing. MUST be set via env var.",
    )
    jwt_ttl_seconds: int = Field(default=604800)  # 7 days
    rob_initial_password: str = Field(
        default="",
        description=(
            "Initial plaintext password for the rob@rob user account. "
            "Only used on first boot when the user does not yet exist."
        ),
    )

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
    coolify_fqdn: str | None = None
    coolify_url: str | None = None

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

    @model_validator(mode="after")
    def _resolve_admin_password_hash(self) -> Settings:
        """Promote ADMIN_PASSWORD_HASH_B64 to admin_password_hash if needed.

        Workaround for env-var platforms (notably Coolify) that perform
        shell-style ``$VAR`` interpolation on values regardless of their
        is_literal flag. bcrypt hashes always contain three ``$`` chars
        (``$2b$<cost>$<salt+digest>``), each of which the platform reads
        as the start of a variable reference — silently dropping
        ``$<chars>`` segments and shipping a truncated hash to the
        container. Base64 has no ``$`` chars and round-trips cleanly.

        After this validator runs ``admin_password_hash`` is always the
        canonical 60-char bcrypt string (or we raise so the deploy fails
        loudly before any login attempt).
        """
        if not self.admin_password_hash and self.admin_password_hash_b64:
            try:
                decoded = base64.b64decode(
                    self.admin_password_hash_b64, validate=True
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(
                    "ADMIN_PASSWORD_HASH_B64 is set but not valid "
                    f"base64-encoded UTF-8: {e}"
                ) from e
            self.admin_password_hash = decoded

        if len(self.admin_password_hash) < 60:
            raise ValueError(
                "admin_password_hash must be at least 60 chars (bcrypt). "
                "Set ADMIN_PASSWORD_HASH directly, or — if your deploy "
                "platform mangles `$` characters — set "
                "ADMIN_PASSWORD_HASH_B64 to the base64 of the hash. "
                f"Got length {len(self.admin_password_hash)}."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor, suitable as a FastAPI dependency."""
    return Settings()
