import asyncio
import contextlib
import logging
import os
import threading
import time
from typing import Any, Optional

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model

logger = logging.getLogger(__name__)


# Layer 4 (resilience): how often we emit ``llm_call_pending`` while a single
# LLM call is outstanding, and how long it must run before we flip
# ``soft_warning`` on the event so the frontend can style it as concerning.
# The 30s baseline catches the real-run-2026-05-22 pattern (first 7 calls
# succeed in <20s each, the 8th hangs) without flooding the SSE stream for
# normal slow models. 90s soft-warning is "long enough that we suspect the
# call is no longer going to come back" — the engine's retry envelope kicks
# in around then anyway.
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_SOFT_WARNING_AFTER = 90.0


class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and capability-aware binding.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling.

    ``with_structured_output`` consults the per-model capability table
    (``capabilities.get_capabilities``) to pick the method and to decide
    whether ``tool_choice`` may be sent. Models that reject ``tool_choice``
    (e.g. DeepSeek V4 and reasoner — per their official tool-calling
    guide) still bind the schema as a tool, but no ``tool_choice``
    parameter is sent.

    Provider-specific quirks beyond structured-output (e.g. DeepSeek's
    reasoning_content roundtrip) live in subclasses so this base class
    stays small.

    Heartbeat (Layer 4): when an ``observer`` is attached via
    :meth:`set_observer`, both ``invoke`` and ``ainvoke`` wrap the call in
    a loop that emits ``llm_call_pending`` events at
    ``HEARTBEAT_INTERVAL_SECONDS`` intervals so the UI can show "still
    waiting on this LLM call (60s elapsed)" instead of going silent for
    the full retry window. With no observer attached, the methods behave
    identically to the bare ``ChatOpenAI`` (CLI / programmatic callers
    pay no cost).
    """

    # Pydantic v2 / langchain-openai treats these as private attributes; they
    # are NOT validated and NOT serialized. We use ``model_config`` already
    # inherited from ChatOpenAI which allows extra="allow".
    _heartbeat_observer: Optional[Any] = None
    _heartbeat_agent_hint: Optional[str] = None

    def set_observer(self, observer: Optional[Any]) -> None:
        """Attach (or detach) the run observer for heartbeat emission.

        The observer must expose ``emit_llm_call_pending(payload: dict)``
        — :class:`app.observers.web_run_observer.WebRunObserver` does.
        Pass ``None`` to detach (used by tests). Setting goes through
        ``object.__setattr__`` because pydantic v2 freezes attribute
        assignment on validated fields by default and these are private
        bookkeeping attributes, not model fields.
        """
        object.__setattr__(self, "_heartbeat_observer", observer)

    def set_agent_hint(self, agent_hint: Optional[str]) -> None:
        """Tag the next call(s) with an agent label for heartbeat events.

        Setup wraps every analyst node with a helper that calls this
        before delegating to the underlying node fn so heartbeats
        emitted during e.g. the Fundamentals Analyst's slow tool-call
        round carry ``agent="Fundamentals Analyst"``.
        """
        object.__setattr__(self, "_heartbeat_agent_hint", agent_hint)

    def invoke(self, input, config=None, **kwargs):
        observer = getattr(self, "_heartbeat_observer", None)
        if observer is None:
            return normalize_content(super().invoke(input, config, **kwargs))
        return normalize_content(
            self._invoke_with_heartbeat(observer, input, config, **kwargs)
        )

    async def ainvoke(self, input, config=None, **kwargs):
        observer = getattr(self, "_heartbeat_observer", None)
        if observer is None:
            return await super().ainvoke(input, config, **kwargs)
        return await self._ainvoke_with_heartbeat(observer, input, config, **kwargs)

    async def _ainvoke_with_heartbeat(
        self, observer: Any, input: Any, config: Any, **kwargs: Any
    ):
        """Async heartbeat wrapper: shield the underlying ``ainvoke`` and emit on timeout.

        ``asyncio.shield`` is the load-bearing primitive — without it, our
        ``wait_for`` cancellation on each interval would cancel the LLM
        task itself, defeating the point of letting it keep running. We
        loop until the shielded task resolves (or until external
        cancellation fires, in which case we propagate after cancelling
        the inner task so the LLM call doesn't keep burning quota in the
        background).
        """
        task = asyncio.ensure_future(super().ainvoke(input, config, **kwargs))
        elapsed = 0.0
        try:
            while True:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=HEARTBEAT_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    elapsed += HEARTBEAT_INTERVAL_SECONDS
                    self._emit_heartbeat(observer, elapsed)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise

    def _invoke_with_heartbeat(
        self, observer: Any, input: Any, config: Any, **kwargs: Any
    ):
        """Sync heartbeat wrapper: run the LLM call in a worker thread, emit while we wait.

        The engine's analyst nodes call ``chain.invoke(...)`` synchronously
        — that's the call site that hung for 37 minutes on 2026-05-22 — so
        the sync path needs heartbeat coverage too. We use a daemon thread
        for the underlying call and use the outer thread (the engine's
        worker thread, which already lives on a non-loop thread) to
        sleep+emit. Exceptions from the inner thread are re-raised in the
        outer thread to preserve the original error path.
        """
        result_container: dict[str, Any] = {}

        def _runner() -> None:
            try:
                result_container["value"] = super(NormalizedChatOpenAI, self).invoke(
                    input, config, **kwargs
                )
            except BaseException as exc:  # noqa: BLE001 — re-raised outer side
                result_container["error"] = exc

        worker = threading.Thread(
            target=_runner, name="llm-invoke", daemon=True
        )
        worker.start()

        elapsed = 0.0
        while True:
            worker.join(timeout=HEARTBEAT_INTERVAL_SECONDS)
            if not worker.is_alive():
                break
            elapsed += HEARTBEAT_INTERVAL_SECONDS
            self._emit_heartbeat(observer, elapsed)

        if "error" in result_container:
            raise result_container["error"]
        return result_container.get("value")

    def _emit_heartbeat(self, observer: Any, elapsed: float) -> None:
        """Build the payload + ship it. Swallow observer errors so the LLM call
        doesn't fail just because the event bus had a hiccup.
        """
        payload = {
            "model": getattr(self, "model_name", None) or "unknown",
            "agent": getattr(self, "_heartbeat_agent_hint", None) or "Engine",
            "elapsed_seconds": int(elapsed) if elapsed.is_integer() else elapsed,
            "soft_warning": elapsed >= HEARTBEAT_SOFT_WARNING_AFTER,
        }
        try:
            observer.emit_llm_call_pending(payload)
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "heartbeat emit failed for model=%s agent=%s",
                payload["model"], payload["agent"],
            )

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # When the model rejects tool_choice, suppress langchain's hardcoded
        # value. The schema is still bound as a tool — exactly what
        # DeepSeek's official tool-calling examples do.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Thinking-mode round-trip is the only DeepSeek-specific behavior that
    stays here. When DeepSeek's thinking models return a response with
    ``reasoning_content``, that field must be echoed back as part of the
    assistant message on the next turn or the API fails with HTTP 400.
    ``_create_chat_result`` captures it on receive and
    ``_get_request_payload`` re-attaches it on send.

    Tool-choice handling for V4 and reasoner — those models reject the
    ``tool_choice`` parameter — is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_)):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", [])
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax-specific overrides on top of the OpenAI-compatible client.

    M2.x reasoning models embed ``<think>...</think>`` blocks directly in
    ``message.content`` by default, which would pollute saved reports.
    Per platform.minimax.io/docs/api-reference/text-openai-api, setting
    ``reasoning_split=True`` in the request body redirects the thinking
    block into ``reasoning_details`` so ``content`` stays clean.

    The flag is gated by ``ModelCapabilities.requires_reasoning_split``
    because non-reasoning MiniMax endpoints (Coding Plan, MiniMax-Text-01)
    reject the parameter via the openai SDK's strict kwarg validation
    (#826).

    Tool-choice handling for M2.x — those models accept only the string
    enum ``{"none", "auto"}`` and reject langchain's function-spec dict —
    is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if get_capabilities(self.model_name).requires_reasoning_split:
            payload.setdefault("reasoning_split", True)
        return payload


# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# Provider base URLs. API-key env vars live in api_key_env.PROVIDER_API_KEY_ENV
# (one canonical mapping consulted by both this client and the CLI's
# interactive key-prompt). Dual-region providers (qwen/glm/minimax) keep
# separate endpoints because international and China accounts cannot share
# credentials (#758).
_PROVIDER_BASE_URL = {
    "xai":        "https://api.x.ai/v1",
    "deepseek":   "https://api.deepseek.com",
    "qwen":       "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "qwen-cn":    "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "glm":        "https://api.z.ai/api/paas/v4/",
    "glm-cn":     "https://open.bigmodel.cn/api/paas/v4/",
    "minimax":    "https://api.minimax.io/v1",
    "minimax-cn": "https://api.minimaxi.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama":     "http://localhost:11434/v1",
}


def _resolve_provider_base_url(provider: str) -> Optional[str]:
    """Default base URL for ``provider``, with env-var overrides where defined.

    Currently only Ollama supports an env-var override (``OLLAMA_BASE_URL``),
    matching the convention in the broader Ollama tooling ecosystem so users
    can point at a remote ollama-serve without editing code. The check is
    call-time, not import-time, so tests that monkeypatch the env after
    import behave correctly.
    """
    if provider == "ollama":
        env_url = os.environ.get("OLLAMA_BASE_URL")
        if env_url:
            return env_url
    return _PROVIDER_BASE_URL.get(provider)


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return configured ChatOpenAI instance."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        # Provider-specific base URL and auth. An explicit base_url on the
        # client (e.g. a corporate proxy) takes precedence over the
        # provider default so users can route through their own gateway.
        if self.provider in _PROVIDER_BASE_URL:
            llm_kwargs["base_url"] = self.base_url or _resolve_provider_base_url(self.provider)
            api_key_env = get_api_key_env(self.provider)
            if api_key_env:
                api_key = os.environ.get(api_key_env)
                if api_key:
                    llm_kwargs["api_key"] = api_key
                else:
                    raise ValueError(
                        f"API key for provider '{self.provider}' is not set. "
                        f"Please set the {api_key_env} environment variable "
                        f"(e.g. add {api_key_env}=your_key to your .env file)."
                    )
            else:
                # Ollama: a local ollama-serve needs no auth — ChatOpenAI still
                # requires a non-empty api_key, so we send the literal "ollama"
                # as a placeholder. Ollama Cloud, by contrast, authenticates via
                # a Bearer token; when OLLAMA_API_KEY is set we forward it so
                # the same provider path covers both local and Cloud usage
                # (paired with OLLAMA_BASE_URL=https://ollama.com/v1).
                llm_kwargs["api_key"] = os.environ.get("OLLAMA_API_KEY") or "ollama"
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # Default max_retries: cloud providers see sustained 5xx bursts
        # (Ollama Cloud, Tongyi/DashScope, OpenRouter passthrough) so the
        # SDK default of 2 burns through retries in well under a second.
        # Native openai stays at 2 because OpenAI's own infra is reliable
        # and extra retries just amplify rate-limit penalties.
        if "max_retries" not in llm_kwargs:
            env_retries = os.environ.get("TRADINGAGENTS_LLM_MAX_RETRIES")
            if env_retries is not None:
                llm_kwargs["max_retries"] = int(env_retries)
            else:
                llm_kwargs["max_retries"] = 2 if self.provider == "openai" else 5

        # Default timeout: httpx's default is 10 minutes, which lets a hung
        # upstream pin the single-concurrent-run lock for the full window.
        # Precedence: env TRADINGAGENTS_LLM_READ_TIMEOUT > per-model
        # capability ``read_timeout_seconds`` > 120s baseline. The env wins
        # because operators set it deliberately deploy-wide; the capability
        # row lets reasoning models (kimi-k2-thinking, gpt-oss, ...) avoid
        # the 120s default that pinned a real run for 37 minutes on
        # 2026-05-22. Connect/write/pool stay tight at 10s either way.
        if "timeout" not in llm_kwargs:
            env_read = os.environ.get("TRADINGAGENTS_LLM_READ_TIMEOUT")
            if env_read is not None:
                read_s = float(env_read)
            else:
                cap_read = get_capabilities(self.model).read_timeout_seconds
                read_s = float(cap_read) if cap_read is not None else 120.0
            llm_kwargs["timeout"] = httpx.Timeout(
                connect=10.0, read=read_s, write=10.0, pool=10.0,
            )

        # Native OpenAI: use Responses API for consistent behavior across
        # all model families. Third-party providers use Chat Completions.
        if self.provider == "openai":
            llm_kwargs["use_responses_api"] = True

        # Provider-specific quirks live in their own subclasses so the
        # base NormalizedChatOpenAI stays free of provider branches.
        if self.provider == "deepseek":
            chat_cls = DeepSeekChatOpenAI
        elif self.provider in ("minimax", "minimax-cn"):
            chat_cls = MinimaxChatOpenAI
        else:
            chat_cls = NormalizedChatOpenAI
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
