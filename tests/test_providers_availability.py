"""Tests for `available_providers()` env-credential filtering.

The autouse `_dummy_api_keys` fixture in `tests/conftest.py` sets every
provider API key env var to ``"placeholder"``. To exercise the
credentials-missing branches we must explicitly `monkeypatch.delenv`
the vars we want unset on a per-test basis.
"""

from __future__ import annotations

import pytest

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.providers import (
    PROVIDERS,
    ProviderSpec,
    available_providers,
)

_ALL_PROVIDER_ENV_VARS: tuple[str, ...] = tuple(
    sorted(
        {env for env in PROVIDER_API_KEY_ENV.values() if env}
        | {"AZURE_OPENAI_ENDPOINT", "OLLAMA_BASE_URL"}
    )
)


def _clear_all_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every provider-credential env var, defeating the autouse fixture."""
    for env_var in _ALL_PROVIDER_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def _spec_for(key: str) -> ProviderSpec:
    for spec in PROVIDERS:
        if spec.key == key:
            return spec
    raise AssertionError(f"no ProviderSpec found for key={key!r}")


def test_zero_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_provider_env(monkeypatch)
    assert available_providers() == ()


def test_only_ollama_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    result = available_providers()

    assert result == (_spec_for("ollama"),)


def test_ollama_plus_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = available_providers()

    assert len(result) == 2
    keys = {spec.key for spec in result}
    assert keys == {"ollama", "openai"}


def test_azure_excluded_when_only_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-test")
    # AZURE_OPENAI_ENDPOINT intentionally NOT set

    result = available_providers()

    assert _spec_for("azure") not in result
    assert result == ()


def test_azure_included_when_both_key_and_endpoint_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-azure-test")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/"
    )

    result = available_providers()

    assert _spec_for("azure") in result


def test_openai_excluded_when_key_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_all_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "")

    result = available_providers()

    assert _spec_for("openai") not in result
    assert result == ()
