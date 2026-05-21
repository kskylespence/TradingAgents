"""Tests for OLLAMA_API_KEY env-var support (Ollama Cloud auth).

Local Ollama needs no auth — the OpenAI-compatible client just sends the
literal string "ollama" as a placeholder api_key. Ollama Cloud requires a
real Bearer token via OLLAMA_API_KEY, while preserving the no-auth
behavior when the variable is unset.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_client():
    import tradingagents.llm_clients.openai_client as mod
    return importlib.reload(mod)


def test_ollama_uses_sentinel_when_api_key_unset(monkeypatch):
    """Local Ollama: no OLLAMA_API_KEY → fall back to the 'ollama' sentinel."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    mod = _reload_client()
    client = mod.OpenAIClient(model="llama3.1", provider="ollama")
    llm = client.get_llm()
    # The ChatOpenAI api_key is a pydantic SecretStr.
    assert llm.openai_api_key.get_secret_value() == "ollama"


def test_ollama_uses_env_when_api_key_set(monkeypatch):
    """Ollama Cloud: OLLAMA_API_KEY env var becomes the Bearer token."""
    monkeypatch.setenv("OLLAMA_API_KEY", "oll-cloud-test-key")
    mod = _reload_client()
    client = mod.OpenAIClient(model="llama3.1", provider="ollama")
    llm = client.get_llm()
    assert llm.openai_api_key.get_secret_value() == "oll-cloud-test-key"


def test_ollama_api_key_does_not_leak_to_other_providers(monkeypatch):
    """Setting OLLAMA_API_KEY must not affect OpenAI/xAI/etc. auth resolution."""
    monkeypatch.setenv("OLLAMA_API_KEY", "oll-should-be-ignored")
    monkeypatch.setenv("XAI_API_KEY", "xai-real-key")
    mod = _reload_client()
    client = mod.OpenAIClient(model="grok-3", provider="xai")
    llm = client.get_llm()
    assert llm.openai_api_key.get_secret_value() == "xai-real-key"


def test_ollama_evaluation_is_call_time(monkeypatch):
    """Env var read must happen at get_llm() time, not import time."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    mod = _reload_client()
    # Set the env AFTER import — the client must still pick it up.
    monkeypatch.setenv("OLLAMA_API_KEY", "set-after-import")
    client = mod.OpenAIClient(model="llama3.1", provider="ollama")
    llm = client.get_llm()
    assert llm.openai_api_key.get_secret_value() == "set-after-import"
