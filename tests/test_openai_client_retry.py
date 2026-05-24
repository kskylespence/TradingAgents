"""Tests for OpenAIClient retry and timeout defaults (Ollama Cloud HTTP-500 fix).

Cloud providers occasionally return sustained transient 5xx bursts (e.g. Ollama
Cloud /v1/chat/completions). The default SDK ``max_retries=2`` with sub-second
backoff is useless against any sustained transient, and the default 10-minute
httpx timeout pins the single-concurrent-run lock when an upstream hangs.

These tests pin:
- Per-provider ``max_retries`` defaults (cloud=5, native openai=2).
- ``TRADINGAGENTS_LLM_MAX_RETRIES`` env override.
- Explicit kwarg wins over env wins over default.
- Default ``httpx.Timeout(connect=10, read=120, write=10, pool=10)``.
- ``TRADINGAGENTS_LLM_READ_TIMEOUT`` env override (only the read field).
- Explicit ``timeout`` kwarg wins over env+default.
"""

from __future__ import annotations

import importlib

import httpx
import pytest


def _reload_client():
    import tradingagents.llm_clients.openai_client as mod
    return importlib.reload(mod)


def _build(provider: str, model: str = "gpt-4.1", **kwargs):
    """Construct a ChatOpenAI through OpenAIClient for the given provider.

    The default ``model`` is intentionally a non-reasoning model so the
    per-provider retry / timeout invariants exercised here are not
    perturbed by the per-model ``read_timeout_seconds`` overrides for
    reasoning models like ``kimi-k2-thinking``. Tests that need a
    reasoning model can pass ``model=`` explicitly.
    """
    mod = _reload_client()
    client = mod.OpenAIClient(model=model, provider=provider, **kwargs)
    return client.get_llm()


@pytest.fixture(autouse=True)
def _clear_retry_env(monkeypatch):
    """Each test starts with no override env vars set."""
    monkeypatch.delenv("TRADINGAGENTS_LLM_MAX_RETRIES", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_LLM_READ_TIMEOUT", raising=False)


class TestMaxRetriesDefault:
    def test_ollama_defaults_to_five(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        llm = _build("ollama")
        assert llm.max_retries == 5

    def test_xai_defaults_to_five(self, monkeypatch):
        monkeypatch.setenv("XAI_API_KEY", "test-key")
        llm = _build("xai")
        assert llm.max_retries == 5

    def test_deepseek_defaults_to_five(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        llm = _build("deepseek")
        assert llm.max_retries == 5

    def test_openrouter_defaults_to_five(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        llm = _build("openrouter")
        assert llm.max_retries == 5

    def test_native_openai_defaults_to_two(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        llm = _build("openai")
        assert llm.max_retries == 2


class TestMaxRetriesEnvOverride:
    def test_env_overrides_cloud_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_MAX_RETRIES", "10")
        llm = _build("ollama")
        assert llm.max_retries == 10

    def test_env_overrides_native_default(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("TRADINGAGENTS_LLM_MAX_RETRIES", "7")
        llm = _build("openai")
        assert llm.max_retries == 7


class TestMaxRetriesExplicitWins:
    def test_explicit_kwarg_wins_over_env(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_MAX_RETRIES", "10")
        llm = _build("ollama", max_retries=3)
        assert llm.max_retries == 3

    def test_explicit_kwarg_wins_over_default(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        llm = _build("ollama", max_retries=1)
        assert llm.max_retries == 1


class TestTimeoutDefault:
    def test_default_timeout_applied_for_ollama(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        llm = _build("ollama")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.connect == 10.0
        assert t.read == 120.0
        assert t.write == 10.0
        assert t.pool == 10.0

    def test_default_timeout_applied_for_native_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        llm = _build("openai")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.connect == 10.0
        assert t.read == 120.0
        assert t.write == 10.0
        assert t.pool == 10.0


class TestTimeoutEnvOverride:
    def test_env_overrides_only_read_field(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_READ_TIMEOUT", "300")
        llm = _build("ollama")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 300.0
        assert t.connect == 10.0
        assert t.write == 10.0
        assert t.pool == 10.0


class TestTimeoutExplicitWins:
    def test_explicit_timeout_kwarg_wins(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.setenv("TRADINGAGENTS_LLM_READ_TIMEOUT", "300")
        explicit = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
        llm = _build("ollama", timeout=explicit)
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.connect == 1.0
        assert t.read == 2.0
        assert t.write == 3.0
        assert t.pool == 4.0
