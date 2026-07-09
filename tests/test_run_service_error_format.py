"""Unit tests for ``_format_engine_error`` (web backend run service).

The helper classifies the exception raised by the engine loop into a
friendly, operator-actionable message that the frontend renders verbatim
into the run row's error banner. These tests pin the wording shape — not
exact strings — so the helper can evolve without forcing test churn.

The test file lives in the top-level ``tests/`` rather than
``web/backend/tests/`` because ``_format_engine_error`` is a pure
function with no FastAPI / DB dependencies. We bolt ``web/backend`` onto
``sys.path`` so the ``app.*`` import resolves the same way the backend
test suite resolves it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BACKEND_ROOT = _REPO_ROOT / "web" / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import httpx  # noqa: E402
import openai  # noqa: E402
from app.services.run_service import _format_engine_error  # noqa: E402

# --------------------------------------------------------------------------- #
# Lightweight exception subclasses                                            #
# --------------------------------------------------------------------------- #
# Real ``openai`` exception classes require constructing a full
# ``httpx.Response`` (status, headers, request) which is fiddly to spell
# in a test. ``isinstance`` checks against the parent class still match
# bare subclasses, so the helper sees these as genuine OpenAI exceptions.


class _FakeInternalServerError(openai.InternalServerError):
    def __init__(self, message: str, status_code: int = 500) -> None:
        Exception.__init__(self, message)
        self.status_code = status_code
        self.message = message

    def __str__(self) -> str:  # repr matches what reaches str(exc) in prod
        return self.message


class _FakeAPIStatusError(openai.APIStatusError):
    def __init__(self, message: str, status_code: int) -> None:
        Exception.__init__(self, message)
        self.status_code = status_code
        self.message = message

    def __str__(self) -> str:
        return self.message


class _FakeAPITimeoutError(openai.APITimeoutError):
    def __init__(self, message: str = "Request timed out") -> None:
        Exception.__init__(self, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class _FakeAPIConnectionError(openai.APIConnectionError):
    def __init__(self, message: str = "Connection error.") -> None:
        Exception.__init__(self, message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class _FakeAuthenticationError(openai.AuthenticationError):
    def __init__(self, message: str = "Invalid API key", status_code: int = 401) -> None:
        Exception.__init__(self, message)
        self.status_code = status_code
        self.message = message

    def __str__(self) -> str:
        return self.message


class _FakeRateLimitError(openai.RateLimitError):
    def __init__(self, message: str = "Too many requests", status_code: int = 429) -> None:
        Exception.__init__(self, message)
        self.status_code = status_code
        self.message = message

    def __str__(self) -> str:
        return self.message


class _FakeBadRequestError(openai.BadRequestError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        Exception.__init__(self, message)
        self.status_code = status_code
        self.message = message

    def __str__(self) -> str:
        return self.message


# --------------------------------------------------------------------------- #
# Table-driven cases                                                          #
# --------------------------------------------------------------------------- #


_OLLAMA_REF_MSG = (
    "Error code: 500 - {'error': 'Internal Server Error "
    "(ref: fd44ca4b-dde3-4004-8dd0-25a6adef1503)'}"
)


CASES = [
    pytest.param(
        _FakeInternalServerError(_OLLAMA_REF_MSG, status_code=500),
        "ollama",
        [
            "Upstream provider error",
            "ollama",
            "HTTP 500",
            "fd44ca4b-dde3-4004-8dd0-25a6adef1503",
            "Retry",
        ],
        id="internal-server-error-with-ref",
    ),
    pytest.param(
        _FakeInternalServerError("Error code: 503 - service unavailable", status_code=503),
        "openai",
        [
            "Upstream provider error",
            "openai",
            "HTTP 503",
            "Reference: n/a",
        ],
        id="internal-server-error-no-ref",
    ),
    pytest.param(
        _FakeAPIStatusError("Error code: 502 - Bad Gateway", status_code=502),
        "openrouter",
        [
            "Upstream provider error",
            "openrouter",
            "HTTP 502",
        ],
        id="api-status-error-5xx",
    ),
    pytest.param(
        _FakeAPITimeoutError("Request timed out after 60s"),
        "anthropic",
        ["timed out", "anthropic"],
        id="api-timeout-error",
    ),
    pytest.param(
        _FakeAPIConnectionError("Connection error."),
        "ollama",
        ["Could not reach", "ollama"],
        id="api-connection-error",
    ),
    pytest.param(
        httpx.ConnectError("connection refused"),
        "ollama",
        ["Could not reach", "ollama"],
        id="httpx-connect-error",
    ),
    pytest.param(
        _FakeAuthenticationError("Invalid API key"),
        "openai",
        ["Authentication failed", "openai"],
        id="authentication-error",
    ),
    pytest.param(
        _FakeRateLimitError("Too many requests"),
        "openai",
        ["Rate limited", "openai"],
        id="rate-limit-error",
    ),
    pytest.param(
        _FakeBadRequestError("Unknown parameter 'foo'"),
        "openai",
        ["Bad request", "openai", "Unknown parameter 'foo'"],
        id="bad-request-error-preserves-detail",
    ),
    pytest.param(
        RuntimeError("boom"),
        "openai",
        ["RuntimeError", "boom"],
        id="generic-runtimeerror-fallback",
    ),
]


@pytest.mark.parametrize("exc, provider, expected_substrings", CASES)
def test_format_engine_error_contains_expected_substrings(
    exc: BaseException, provider: str, expected_substrings: list[str]
) -> None:
    out = _format_engine_error(exc, provider)
    for needle in expected_substrings:
        assert needle in out, (
            f"expected substring {needle!r} in formatted output, got: {out!r}"
        )


def test_format_engine_error_prefers_internal_server_over_authentication() -> None:
    # AuthenticationError is a subclass of APIStatusError. Even when its
    # status code is mocked to >=500 (nonsensical, but defensive), the
    # ordering must still classify it as "Authentication failed", not
    # "Upstream provider error", because the helper checks
    # InternalServerError as the first branch by *type*.
    exc = _FakeAuthenticationError("nope", status_code=500)
    out = _format_engine_error(exc, "openai")
    assert "Authentication failed" in out


def test_format_engine_error_status_code_falls_back_when_missing() -> None:
    # Guard against an APIStatusError-shaped exception with no status_code
    # attribute — the helper must not crash.
    exc = _FakeAPIStatusError("kaboom", status_code=500)
    delattr(exc, "status_code")
    out = _format_engine_error(exc, "openai")
    # Either the generic fallback OR a sensible default. Don't crash.
    assert isinstance(out, str) and out
