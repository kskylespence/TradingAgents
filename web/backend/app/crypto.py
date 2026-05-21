"""Fernet encrypt/decrypt helpers for stored API keys.

The master key is read from settings (`FERNET_KEY` env var). For
development, if the key is empty, helpers raise — callers should never
silently store plaintext.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class FernetNotConfiguredError(RuntimeError):
    """FERNET_KEY is unset or invalid."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    settings = get_settings()
    key = settings.fernet_key
    if not key:
        raise FernetNotConfiguredError(
            "FERNET_KEY env var is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise FernetNotConfiguredError(
            f"FERNET_KEY is malformed (expected 32 url-safe base64 bytes): {e}"
        ) from e


def encrypt(plaintext: str) -> bytes:
    """Encrypt a string; returns Fernet ciphertext bytes for storage."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    """Decrypt Fernet ciphertext bytes back to a string.

    Raises:
        FernetNotConfiguredError: if FERNET_KEY is unset/invalid.
        InvalidToken: if the ciphertext doesn't verify (forged or wrong key).
    """
    return _get_fernet().decrypt(ciphertext).decode("utf-8")


def reset_cache() -> None:
    """Drop the cached Fernet instance. Used in tests after rotating keys."""
    _get_fernet.cache_clear()


__all__ = [
    "encrypt",
    "decrypt",
    "reset_cache",
    "FernetNotConfiguredError",
    "InvalidToken",
]
