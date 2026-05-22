"""Fix C: LangGraph node-level retry policy for transient LLM/network errors.

A CRM analysis run failed when Ollama Cloud returned HTTP 500 from
`/v1/chat/completions`. The OpenAI SDK retried twice with sub-second backoff,
all failed, and the exception killed the entire LangGraph run. These tests
assert that a node-level RetryPolicy is attached so transient errors get a
second resilience layer on top of SDK retries.
"""

from __future__ import annotations

import time
from typing import TypedDict

import httpx
import openai
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from tradingagents.graph.trading_graph import _TRANSIENT_RETRY_POLICY


class _State(TypedDict):
    n: int


def _make_internal_server_error() -> openai.InternalServerError:
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    resp = httpx.Response(500, request=req)
    return openai.InternalServerError("Error code: 500", response=resp, body=None)


def _fast_policy() -> RetryPolicy:
    """A clone of _TRANSIENT_RETRY_POLICY with near-zero interval for tests.

    RetryPolicy is a NamedTuple (immutable), so we can't monkeypatch
    initial_interval on the shared constant; we build a sibling that keeps the
    same retry_on tuple. The behaviour we want to assert lives in retry_on and
    max_attempts, not the wall-clock interval.
    """
    return RetryPolicy(
        max_attempts=_TRANSIENT_RETRY_POLICY.max_attempts,
        initial_interval=0.001,
        backoff_factor=_TRANSIENT_RETRY_POLICY.backoff_factor,
        jitter=False,
        retry_on=_TRANSIENT_RETRY_POLICY.retry_on,
    )


def _build_one_node_graph(node_fn, policy):
    sg = StateGraph(_State)
    sg.add_node("only", node_fn, retry_policy=policy)
    sg.add_edge(START, "only")
    sg.add_edge("only", END)
    return sg.compile()


class TestTransientRetryPolicy:
    def test_policy_settings(self):
        """The constant must match the team-lead's spec: 3 attempts, 8s base, x2 backoff, jitter."""
        p = _TRANSIENT_RETRY_POLICY
        assert p.max_attempts == 3
        assert p.initial_interval == 8.0
        assert p.backoff_factor == 2.0
        assert p.jitter is True

    def test_policy_retries_on_transient_classes(self):
        """retry_on must include the four transient classes we care about."""
        retry_on = _TRANSIENT_RETRY_POLICY.retry_on
        if callable(retry_on) and not isinstance(retry_on, type):
            # If implemented as a predicate, verify behaviour instead of identity.
            req = httpx.Request("POST", "https://example.com/v1/chat/completions")
            resp = httpx.Response(500, request=req)
            assert retry_on(openai.InternalServerError("e", response=resp, body=None))
            assert retry_on(openai.APITimeoutError(request=req))
            assert retry_on(openai.APIConnectionError(request=req))
            assert retry_on(httpx.RemoteProtocolError("e", request=req))
            assert not retry_on(ValueError("nope"))
        else:
            classes = tuple(retry_on) if not isinstance(retry_on, type) else (retry_on,)
            assert openai.InternalServerError in classes
            assert openai.APITimeoutError in classes
            assert openai.APIConnectionError in classes
            assert httpx.RemoteProtocolError in classes

    def test_internal_server_error_is_retried_then_succeeds(self):
        """LangGraph retries on openai.InternalServerError and the run completes on attempt 2."""
        attempts = {"n": 0}

        def flaky(state: _State) -> _State:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _make_internal_server_error()
            return {"n": attempts["n"]}

        graph = _build_one_node_graph(flaky, _fast_policy())
        out = graph.invoke({"n": 0})

        assert attempts["n"] == 2
        assert out == {"n": 2}

    def test_persistent_internal_server_error_exhausts_retries(self):
        """Persistent transient failure must bubble after max_attempts, not loop forever."""
        attempts = {"n": 0}

        def always_500(state: _State) -> _State:
            attempts["n"] += 1
            raise _make_internal_server_error()

        graph = _build_one_node_graph(always_500, _fast_policy())

        start = time.monotonic()
        with pytest.raises(openai.InternalServerError):
            graph.invoke({"n": 0})
        elapsed = time.monotonic() - start

        # max_attempts=3 means at most 3 calls of the node function.
        assert attempts["n"] == 3
        # Should fail well within a few seconds at the fast interval.
        assert elapsed < 10.0

    def test_non_transient_is_not_retried(self):
        """ValueError is not in retry_on — must bubble immediately, no retry attempt."""
        attempts = {"n": 0}

        def bad_input(state: _State) -> _State:
            attempts["n"] += 1
            raise ValueError("bad input")

        graph = _build_one_node_graph(bad_input, _fast_policy())

        with pytest.raises(ValueError, match="bad input"):
            graph.invoke({"n": 0})

        assert attempts["n"] == 1

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda req: openai.APITimeoutError(request=req),
            lambda req: openai.APIConnectionError(request=req),
            lambda req: httpx.RemoteProtocolError("server disconnected", request=req),
        ],
        ids=["APITimeoutError", "APIConnectionError", "RemoteProtocolError"],
    )
    def test_other_transient_classes_are_retried(self, exc_factory):
        req = httpx.Request("POST", "https://example.com/v1/chat/completions")
        attempts = {"n": 0}

        def flaky(state: _State) -> _State:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise exc_factory(req)
            return {"n": attempts["n"]}

        graph = _build_one_node_graph(flaky, _fast_policy())
        out = graph.invoke({"n": 0})
        assert attempts["n"] == 2
        assert out == {"n": 2}


class TestCompileSitesUsePolicy:
    """The three workflow.compile() sites in TradingAgentsGraph must apply the policy.

    Since the installed LangGraph version attaches retry_policy on nodes (not on
    compile), the production code must mutate workflow.nodes to set retry_policy
    on every node before compiling. These tests inspect that the policy is set.
    """

    def test_setup_graph_nodes_get_retry_policy_after_attach(self):
        """When _attach_retry_policy is called on a workflow, every node gets the policy."""
        from tradingagents.graph.trading_graph import _attach_retry_policy

        sg = StateGraph(_State)
        sg.add_node("a", lambda s: s)
        sg.add_node("b", lambda s: s)

        _attach_retry_policy(sg)

        # RetryPolicy is itself a NamedTuple, so test isinstance(RetryPolicy) directly
        # rather than the tuple-or-single dance.
        for name, spec in sg.nodes.items():
            policy = spec.retry_policy
            if isinstance(policy, RetryPolicy):
                assert policy is _TRANSIENT_RETRY_POLICY, f"node {name!r} missing policy"
            else:
                # Sequence form
                assert len(policy) >= 1, f"node {name!r} has empty policy sequence"
                assert policy[0] is _TRANSIENT_RETRY_POLICY
