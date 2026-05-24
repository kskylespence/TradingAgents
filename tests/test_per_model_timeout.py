"""Tests for per-model ``read_timeout_seconds`` capability (Layer 2 resilience).

A real run on 2026-05-22 hung for 37 minutes inside a single LangGraph node
because the LLM call's read timeout was 120s and ``kimi-k2-thinking``
legitimately needed 2-4 minutes to respond. The global 120s default is too
tight for reasoning models. Per-model overrides via the capability table let
healthy slow models work while keeping fast models on a tight budget.

Precedence pinned by these tests:
    env ``TRADINGAGENTS_LLM_READ_TIMEOUT`` > capability ``read_timeout_seconds`` > 120s default

The env wins because operators set it deliberately deploy-wide.
"""

from __future__ import annotations

import importlib

import httpx
import pytest

from tradingagents.llm_clients.capabilities import (
    ModelCapabilities,
    get_capabilities,
)


def _reload_client():
    import tradingagents.llm_clients.openai_client as mod
    return importlib.reload(mod)


def _build(model: str, provider: str = "ollama", **kwargs):
    """Construct a ChatOpenAI through OpenAIClient for the given model+provider."""
    mod = _reload_client()
    client = mod.OpenAIClient(model=model, provider=provider, **kwargs)
    return client.get_llm()


@pytest.fixture(autouse=True)
def _clear_timeout_env(monkeypatch):
    """Each test starts with no override env vars set.

    Also force-sets the auth env vars for providers we construct. The
    repo-wide conftest's autouse ``_dummy_api_keys`` defers to the host
    env when present, but on this dev box ``ZHIPU_API_KEY`` is exported
    as the empty string, which the openai_client treats as missing.
    """
    monkeypatch.delenv("TRADINGAGENTS_LLM_READ_TIMEOUT", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    # Defensive: force placeholder over any empty-string host value so the
    # OpenAIClient API-key gate doesn't trip on providers we exercise here.
    monkeypatch.setenv("ZHIPU_API_KEY", "placeholder")


@pytest.mark.unit
class TestPerModelReadTimeout:
    """Reasoning models get a longer read timeout via the capability table."""

    def test_kimi_k2_thinking_uses_300s_read_timeout(self):
        llm = _build("kimi-k2-thinking")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 300.0
        # Other fields stay tight.
        assert t.connect == 10.0
        assert t.write == 10.0
        assert t.pool == 10.0

    def test_glm_5_uses_120s_default_read_timeout(self):
        llm = _build("glm-5", provider="glm")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 120.0

    def test_gpt_oss_120b_uses_300s_read_timeout(self):
        llm = _build("gpt-oss:120b")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 300.0


@pytest.mark.unit
class TestEnvOverridesCapability:
    """Env var beats capability table — operators set it deliberately."""

    def test_env_var_overrides_per_model_timeout(self, monkeypatch):
        monkeypatch.setenv("TRADINGAGENTS_LLM_READ_TIMEOUT", "60")
        llm = _build("kimi-k2-thinking")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        # Env (60) wins over the capability default (300) for this model.
        assert t.read == 60.0


@pytest.mark.unit
class TestUnknownModelFallback:
    """Models with no capability row keep the 120s baseline."""

    def test_unknown_model_uses_120s_default(self):
        llm = _build("made-up-model:99")
        t = llm.request_timeout
        assert isinstance(t, httpx.Timeout)
        assert t.read == 120.0


@pytest.mark.unit
class TestCapabilitiesBackCompat:
    """The new field is optional so existing call sites stay valid."""

    def test_model_capabilities_construct_without_read_timeout(self):
        # Mirror the constructor signature used in capabilities.py rows —
        # this must keep working without supplying read_timeout_seconds.
        caps = ModelCapabilities(
            supports_tool_choice=True,
            supports_json_mode=True,
            supports_json_schema=True,
            preferred_structured_method="function_calling",
        )
        assert caps.read_timeout_seconds is None


@pytest.mark.unit
class TestPatternMatchPicksUpReasoningModels:
    """Forward-compat regexes catch new kimi-k2 and ``*:thinking`` variants."""

    def test_pattern_match_picks_up_reasoning_models(self):
        # Any unrecognised kimi-k2 variant inherits the 300s read timeout via
        # _BY_PATTERN — new model IDs land without code changes.
        caps = get_capabilities("kimi-k2-future-model")
        assert caps.read_timeout_seconds == 300

    def test_thinking_suffix_pattern_picks_up_new_reasoning_models(self):
        # Any ``something:thinking`` variant served via Ollama gets the long
        # read timeout via the forward-compat pattern.
        caps = get_capabilities("brand-new-reasoner:thinking")
        assert caps.read_timeout_seconds == 300
