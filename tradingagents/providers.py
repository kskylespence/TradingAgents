"""Shared LLM provider catalog for CLI selections and web form rendering.

Single source of truth for the supported provider list, including the
regional variants (qwen-cn, glm-cn, minimax-cn) that the CLI presents
through a secondary region prompt. Ollama is included with a `None`
default_base_url so callers can resolve `OLLAMA_BASE_URL` at call time
rather than baking the value at import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    display: str
    default_base_url: Optional[str]


PROVIDERS: Tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "OpenAI", "https://api.openai.com/v1"),
    ProviderSpec("google", "Google", None),
    ProviderSpec("anthropic", "Anthropic", "https://api.anthropic.com/"),
    ProviderSpec("xai", "xAI", "https://api.x.ai/v1"),
    ProviderSpec("deepseek", "DeepSeek", "https://api.deepseek.com"),
    ProviderSpec(
        "qwen", "Qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    ),
    ProviderSpec(
        "qwen-cn",
        "Qwen — China",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    ProviderSpec("glm", "GLM", "https://open.bigmodel.cn/api/paas/v4/"),
    ProviderSpec(
        "glm-cn",
        "GLM — BigModel (China)",
        "https://open.bigmodel.cn/api/paas/v4/",
    ),
    ProviderSpec("minimax", "MiniMax", "https://api.minimax.io/v1"),
    ProviderSpec("minimax-cn", "MiniMax — China", "https://api.minimaxi.com/v1"),
    ProviderSpec("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    ProviderSpec("azure", "Azure OpenAI", None),
    ProviderSpec("ollama", "Ollama", None),
)


def get_ollama_base_url() -> str:
    """Resolve the Ollama endpoint at call time.

    Ollama users can point at a remote ollama-serve via OLLAMA_BASE_URL
    (convention from the broader Ollama ecosystem); falls back to the
    localhost default when unset.
    """
    return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
