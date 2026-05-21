"""Failure-mode tests for `app.services.ollama_models.list_ollama_models`.

The catalog endpoint depends on this service and must stay responsive
even when Ollama is unreachable. The contract:

- After a successful fetch, a later failure (HTTP 5xx, connect error,
  timeout) returns the last-good cached list.
- With no prior success, failures return `[]`. The function never raises.
- An auth failure (401) on the first call is treated the same — empty
  list, not a crash.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Helpers (mirror test_ollama_models_service.py)                              #
# --------------------------------------------------------------------------- #
# `_reset_ollama_cache` (autouse) is in `conftest.py`. This file keeps its
# own scripted-client helper because it tests sequential success-then-failure
# behavior that requires walking through a multi-step script — beyond the
# scope of the shared `install_fake_httpx_ollama` helper.


class _FakeResponse:
    def __init__(self, json_data: Any, status: int = 200) -> None:
        self._json = json_data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        return self._json


def _install_scripted_client(
    monkeypatch: pytest.MonkeyPatch, *script: dict[str, Any]
) -> dict[str, Any]:
    """Install a `httpx.AsyncClient` stub that walks through `script`.

    Each entry is either::

        {"json": ..., "status": 200}     # successful response
        {"raise": ConnectError("boom")}   # exception on `get`

    A `stats["calls"]` counter tracks invocations.
    """
    state = {"calls": 0, "last_url": None, "last_headers": None}
    queue = list(script)

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self, url: str, headers: dict[str, str] | None = None
        ) -> _FakeResponse:
            state["calls"] += 1
            state["last_url"] = url
            state["last_headers"] = headers
            if not queue:
                raise AssertionError(
                    f"Unexpected extra HTTP call #{state['calls']} to {url}"
                )
            step = queue.pop(0)
            if "raise" in step:
                raise step["raise"]
            return _FakeResponse(step.get("json"), status=step.get("status", 200))

    from app.services import ollama_models

    monkeypatch.setattr(ollama_models.httpx, "AsyncClient", _FakeClient)
    return state


def _expire_cache() -> None:
    """Force the next call to bypass the TTL cache.

    The service caches by `base_url`; rewriting the cache entry to an
    ancient timestamp is the cleanest way to simulate "TTL elapsed"
    without sleeping or monkeypatching `time.monotonic`.
    """
    from app.services import ollama_models

    for key, (_ts, models) in list(ollama_models._cache.items()):
        ollama_models._cache[key] = (0.0, models)


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_after_success_failure_returns_last_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1st call succeeds → 2nd (after TTL) gets 500 → cached list returned."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_scripted_client(
        monkeypatch,
        {"json": {"data": [{"id": "m1"}, {"id": "m2"}]}},
        {"json": {"error": "kaboom"}, "status": 500},
    )

    from app.services.ollama_models import list_ollama_models

    first = await list_ollama_models()
    assert first == ["m1", "m2"]

    _expire_cache()

    second = await list_ollama_models()
    assert second == ["m1", "m2"], "failure after success must return last-good"
    assert stats["calls"] == 2


async def test_no_prior_success_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start ConnectError → empty list, no exception bubbles up."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_scripted_client(
        monkeypatch,
        {"raise": httpx.ConnectError("connection refused")},
    )

    from app.services.ollama_models import list_ollama_models

    # Must not raise.
    models = await list_ollama_models()
    assert models == []


async def test_401_returns_empty_initially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-start 401 (bad / missing API key) → empty list, no crash."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "wrong-key")

    _install_scripted_client(
        monkeypatch,
        {"json": {"error": "unauthorized"}, "status": 401},
    )

    from app.services.ollama_models import list_ollama_models

    models = await list_ollama_models()
    assert models == []


async def test_failure_does_not_poison_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed fetch must NOT overwrite a previously cached good result.

    After cache expiry: fetch fails → return last-good. A third call,
    still within the (just-extended? — no, NOT extended) TTL of the
    original good result, should still see the good cache OR refetch —
    either way, the cached good list must still be the source of truth.

    The behavior we lock in here: a failure is not allowed to write `[]`
    over the previously-good cache entry. This is what protects the
    catalog from flickering empty during transient upstream blips.
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_scripted_client(
        monkeypatch,
        {"json": {"data": [{"id": "good"}]}},
        {"raise": httpx.ReadTimeout("slow")},
        {"raise": httpx.ConnectError("down")},
    )

    from app.services import ollama_models
    from app.services.ollama_models import list_ollama_models

    assert await list_ollama_models() == ["good"]

    _expire_cache()
    assert await list_ollama_models() == ["good"]

    # Cache entry should still record the good list, not an empty one
    # injected by the failure path.
    cached_models = ollama_models._cache[
        "https://ollama.example.com/v1"
    ][1]
    assert cached_models == ["good"]

    _expire_cache()
    assert await list_ollama_models() == ["good"]
