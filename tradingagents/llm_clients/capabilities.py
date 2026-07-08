"""Declarative per-model capability table for OpenAI-compatible providers.

This is the single place that knows which model IDs reject which API
parameters or require which structured-output method. The LLM client
subclasses consult ``get_capabilities(model_name)`` instead of hardcoding
model-name ``if`` ladders, so adding a new model (or a new provider quirk)
means editing this table — not the client code.

Pattern adapted from the per-model ``compat:`` flags DeepSeek themselves
publish in their integration guides (e.g. the Oh My Pi config schema
documents ``supportsToolChoice``, ``requiresReasoningContentForToolCalls``
as declarative per-model fields).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

StructuredMethod = Literal[
    "function_calling",  # uses tools; respects supports_tool_choice
    "json_mode",         # uses response_format={"type":"json_object"}
    "json_schema",       # uses response_format={"type":"json_schema",...}
    "none",              # no structured output available; caller falls back to free-text
]


@dataclass(frozen=True)
class ModelCapabilities:
    """What an OpenAI-compatible model accepts at the API level."""

    supports_tool_choice: bool
    supports_json_mode: bool
    supports_json_schema: bool
    preferred_structured_method: StructuredMethod
    # DeepSeek thinking-mode models 400 if reasoning_content from prior
    # assistant turns is not echoed back on the next request.
    requires_reasoning_content_roundtrip: bool = False
    # MiniMax M2.x reasoning models need ``reasoning_split=True`` so the
    # <think> block lands in ``reasoning_details`` instead of polluting
    # ``content``. The flag is rejected by non-reasoning MiniMax models
    # (Coding Plan, MiniMax-Text-01, etc.), so we only set it where the
    # model actually consumes it. (#826)
    requires_reasoning_split: bool = False
    # Per-model read timeout. Reasoning models (kimi-k2-thinking, gpt-oss,
    # deepseek-v3.2, ...) legitimately take 2-4 minutes per call; the
    # global 120s default is hostile to them. ``None`` means "use the
    # default", letting fast models stay on the tight budget. The
    # ``TRADINGAGENTS_LLM_READ_TIMEOUT`` env var still wins over this
    # because operators set it deploy-wide.
    read_timeout_seconds: int | None = None


# DeepSeek's thinking models accept the ``tools`` array but reject the
# ``tool_choice`` parameter (official Oh My Pi integration guide and the
# 400 response in issue #678). Their official tool-calling examples
# (api-docs.deepseek.com/guides/tool_calls) pass ``tools=[...]`` without
# ``tool_choice`` — we mirror that pattern by setting supports_tool_choice
# to False and letting the client suppress the kwarg.
_DEEPSEEK_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_content_roundtrip=True,
)

_DEEPSEEK_CHAT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
)

# MiniMax M2.x reasoning models accept the tools array, but their
# tool_choice parameter is restricted to the enum {"none", "auto"}
# (platform.minimax.io/docs/api-reference/text-post). Langchain's
# function_calling path sends tool_choice as a function-spec dict, which
# MiniMax 400s — same shape as the DeepSeek bug. supports_tool_choice=False
# makes the dispatch in NormalizedChatOpenAI suppress the kwarg; the schema
# still ships as a tool. json_mode response_format is only for
# MiniMax-Text-01, not M2.x.
_MINIMAX_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=False,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_split=True,
)

_DEFAULT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=True,
    preferred_structured_method="function_calling",
)

# Reasoning models (Kimi K2 family, GPT-OSS, future ``*:thinking`` variants)
# observe normal API quirks but routinely need 2-4 minutes per call. The
# global 120s default was the proximate cause of a 37-minute pipeline hang
# on 2026-05-22, so these models carry a 300s read budget through the
# capability table.
_REASONING_LONG_TIMEOUT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=True,
    preferred_structured_method="function_calling",
    read_timeout_seconds=300,
)

# DeepSeek V3.2 is a thinking-mode model with the same API quirks as the
# rest of the V3/V4 lineup (no tool_choice, reasoning roundtrip) but a
# tighter 240s read budget — DeepSeek's own latency SLO is meaningfully
# better than Kimi K2's.
_DEEPSEEK_V3_2 = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_content_roundtrip=True,
    read_timeout_seconds=240,
)


# Exact-ID matches take precedence over pattern matches.
_BY_ID: dict[str, ModelCapabilities] = {
    "deepseek-chat": _DEEPSEEK_CHAT,
    "deepseek-reasoner": _DEEPSEEK_THINKING,
    "deepseek-v3.2": _DEEPSEEK_V3_2,
    "deepseek-v4-flash": _DEEPSEEK_THINKING,
    "deepseek-v4-pro": _DEEPSEEK_THINKING,
    # MiniMax — full official model lineup per
    # platform.minimax.io/docs/api-reference/text-openai-api
    "MiniMax-M2.7": _MINIMAX_THINKING,
    "MiniMax-M2.7-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.5": _MINIMAX_THINKING,
    "MiniMax-M2.5-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.1": _MINIMAX_THINKING,
    "MiniMax-M2.1-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2": _MINIMAX_THINKING,
    # Reasoning models with long inference latency. The 300s read budget
    # exists because a real run on 2026-05-22 hung for 37 minutes inside
    # one LangGraph node when kimi-k2-thinking could not finish under the
    # 120s global default. The flags otherwise match the permissive
    # default — these models accept the standard OpenAI tool-calling
    # shape served via Ollama Cloud.
    "kimi-k2-thinking": _REASONING_LONG_TIMEOUT,
    "kimi-k2.5": _REASONING_LONG_TIMEOUT,
    "kimi-k2.6": _REASONING_LONG_TIMEOUT,
    "gpt-oss:120b": _REASONING_LONG_TIMEOUT,
    "gpt-oss:20b": _REASONING_LONG_TIMEOUT,
}

# Forward-compat patterns. New ``deepseek-v5-*`` / ``deepseek-reasoner-*``
# or ``MiniMax-M3*`` variants inherit the thinking-mode quirks automatically.
# Kimi K2 successors and any model whose ID ends in ``:thinking`` inherit
# the 300s read budget by default.
_BY_PATTERN: list[tuple[re.Pattern[str], ModelCapabilities]] = [
    (re.compile(r"^deepseek-v\d"), _DEEPSEEK_THINKING),
    (re.compile(r"^deepseek-reasoner"), _DEEPSEEK_THINKING),
    (re.compile(r"^MiniMax-M\d"), _MINIMAX_THINKING),
    (re.compile(r"^kimi-k2"), _REASONING_LONG_TIMEOUT),
    # ``.+`` is needed because ``get_capabilities`` uses ``re.match`` which
    # anchors at position 0; a bare ``:thinking$`` would only match a model
    # named literally ``:thinking``.
    (re.compile(r"^.+:thinking$"), _REASONING_LONG_TIMEOUT),
]


def get_capabilities(model_name: str) -> ModelCapabilities:
    """Resolve capabilities by exact ID, then pattern, then default."""
    if model_name in _BY_ID:
        return _BY_ID[model_name]
    for pattern, caps in _BY_PATTERN:
        if pattern.match(model_name):
            return caps
    return _DEFAULT
