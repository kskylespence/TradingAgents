"""Tests for `app.services.ollama_models.list_ollama_models`.

The service:
- Calls `GET {OLLAMA_BASE_URL}/models` with optional `Authorization`.
- Parses the OpenAI-shaped `{"data": [{"id": "..."}]}` payload.
- Caches successful results for 5 minutes, keyed by `base_url`.
- Returns the last-good cached list on failure, `[]` if no prior success.
- Never raises.

`httpx.AsyncClient` is monkeypatched per-test to a fake — full TestClient
is overkill for a pure service module that talks outbound to an external
endpoint.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
# `_reset_ollama_cache` (autouse) lives in `conftest.py` — shared with every
# test that touches the Ollama discovery service. This file keeps its own
# `_install_fake_client` instead of using the shared one because the service
# tests need full control of the response JSON (malformed-item edge cases,
# missing `data` keys) that the simpler shared helper deliberately abstracts.


class _FakeResponse:
    """Minimal `httpx.Response` stand-in covering what the service uses."""

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


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_json: Any = None,
    status: int = 200,
    raise_exc: BaseException | None = None,
) -> dict[str, Any]:
    """Replace `httpx.AsyncClient` in the service module with a recording stub.

    Returns a `stats` dict the test can inspect:
      - `calls`: how many `get(...)` invocations happened
      - `last_url`: URL passed to the last `get`
      - `last_headers`: headers dict from the last `get`
    """
    stats: dict[str, Any] = {"calls": 0, "last_url": None, "last_headers": None}

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Accept and ignore `timeout=...` etc. — we don't validate construction.
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self, url: str, headers: dict[str, str] | None = None
        ) -> _FakeResponse:
            stats["calls"] += 1
            stats["last_url"] = url
            stats["last_headers"] = headers
            if raise_exc is not None:
                raise raise_exc
            return _FakeResponse(response_json, status=status)

    from app.services import ollama_models

    monkeypatch.setattr(ollama_models.httpx, "AsyncClient", _FakeClient)
    return stats


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_lists_models_from_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path — OpenAI-shaped response is parsed to a list of ids."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_fake_client(
        monkeypatch,
        response_json={
            "object": "list",
            "data": [
                {"id": "gpt-oss:120b", "object": "model"},
                {"id": "qwen3-coder:480b"},
            ],
        },
    )

    from app.services.ollama_models import list_ollama_models

    models = await list_ollama_models()
    assert models == ["gpt-oss:120b", "qwen3-coder:480b"]


async def test_caches_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call inside the TTL must not hit the network."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_fake_client(
        monkeypatch,
        response_json={"data": [{"id": "model-a"}]},
    )

    from app.services.ollama_models import list_ollama_models

    first = await list_ollama_models()
    second = await list_ollama_models()

    assert first == ["model-a"]
    assert second == ["model-a"]
    assert stats["calls"] == 1, "second call within TTL should be served from cache"


async def test_cache_key_is_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different `OLLAMA_BASE_URL` values must NOT share the cache slot."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    monkeypatch.setenv("OLLAMA_BASE_URL", "https://a.example.com/v1")
    stats = _install_fake_client(
        monkeypatch,
        response_json={"data": [{"id": "from-a"}]},
    )

    from app.services.ollama_models import list_ollama_models

    first = await list_ollama_models()
    assert first == ["from-a"]
    assert stats["calls"] == 1

    # Swap base url → fresh cache slot → second fetch must happen.
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://b.example.com/v1")
    second = await list_ollama_models()
    assert second == ["from-a"]  # fake client returns same payload
    assert stats["calls"] == 2, "changing OLLAMA_BASE_URL should force a refetch"
    assert "b.example.com" in stats["last_url"]


async def test_empty_response_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `data: []` is a success — it must be cached, not retried."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_fake_client(
        monkeypatch,
        response_json={"object": "list", "data": []},
    )

    from app.services.ollama_models import list_ollama_models

    first = await list_ollama_models()
    second = await list_ollama_models()

    assert first == []
    assert second == []
    assert stats["calls"] == 1, "empty list is a successful response — cache it"


async def test_no_auth_header_when_api_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local `ollama serve` requires no API key — request must still go out."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_fake_client(
        monkeypatch,
        response_json={"data": [{"id": "llama3:8b"}]},
    )

    from app.services.ollama_models import list_ollama_models

    models = await list_ollama_models()
    assert models == ["llama3:8b"]
    assert stats["calls"] == 1
    headers = stats["last_headers"] or {}
    assert "Authorization" not in headers, (
        "no Authorization header should be sent when OLLAMA_API_KEY is unset"
    )


async def test_auth_header_when_api_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """With `OLLAMA_API_KEY`, header must be `Bearer <key>`."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.com/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret-key-xyz")

    stats = _install_fake_client(
        monkeypatch,
        response_json={"data": [{"id": "qwen3-coder:480b"}]},
    )

    from app.services.ollama_models import list_ollama_models

    await list_ollama_models()
    headers = stats["last_headers"] or {}
    assert headers.get("Authorization") == "Bearer secret-key-xyz"


async def test_url_appends_models_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GET URL must be `{base_url}/models` (trailing slash handled)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1/")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    stats = _install_fake_client(monkeypatch, response_json={"data": []})

    from app.services.ollama_models import list_ollama_models

    await list_ollama_models()
    assert stats["last_url"] == "https://ollama.example.com/v1/models"


async def test_malformed_items_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries without a string `id` must be dropped silently — never raise."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    _install_fake_client(
        monkeypatch,
        response_json={
            "data": [
                {"id": "good-model"},
                {"object": "model"},  # missing id
                "not-a-dict",  # wrong type
                {"id": 12345},  # non-string id
                {"id": "another-good"},
            ]
        },
    )

    from app.services.ollama_models import list_ollama_models

    models = await list_ollama_models()
    assert models == ["good-model", "another-good"]


# --------------------------------------------------------------------------- #
# last_probe_status — used by the health endpoint to distinguish              #
# "0 models because upstream said so" from "0 models because we failed".      #
# --------------------------------------------------------------------------- #


async def test_last_probe_status_unknown_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fetch attempted yet -> status is 'unknown'."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    from app.services.ollama_models import last_probe_status

    status, error = last_probe_status()
    assert status == "unknown"
    assert error is None


async def test_last_probe_status_ok_after_successful_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful fetch (even with empty data) -> status is 'ok'."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    _install_fake_client(monkeypatch, response_json={"data": []})

    from app.services.ollama_models import last_probe_status, list_ollama_models

    await list_ollama_models()
    status, error = last_probe_status()
    assert status == "ok"
    assert error is None


async def test_last_probe_status_down_after_failed_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed fetch -> status is 'down' and error carries the repr."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://ollama.example.com/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))

    from app.services.ollama_models import last_probe_status, list_ollama_models

    await list_ollama_models()
    status, error = last_probe_status()
    assert status == "down"
    assert error is not None and "boom" in error


async def test_last_probe_status_keyed_by_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each base_url tracks its own attempt status — changing env resets."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://succeeds.example.com/v1")
    _install_fake_client(monkeypatch, response_json={"data": [{"id": "x"}]})

    from app.services.ollama_models import last_probe_status, list_ollama_models

    await list_ollama_models()
    assert last_probe_status()[0] == "ok"

    # Swap base_url to one we haven't probed yet → unknown.
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://never-probed.example.com/v1")
    assert last_probe_status()[0] == "unknown"
