"""Tests for ``app.services.env_inject.scope``.

Covers the four restoration paths the runner depends on:

1. Success — previously-unset var removed.
2. Success — previously-set var restored to its original value.
3. Exception inside the block — env restored.
4. ``asyncio.CancelledError`` inside the block — env restored. The plan
   calls this one out explicitly because it's the failure mode for a
   user clicking *Cancel* mid-run.

Tests use ``monkeypatch.setenv`` / ``monkeypatch.delenv`` for any
pre-existing-var fixtures so the suite is hermetic — pytest's teardown
restores the original environment regardless of any leak we might
introduce.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from app.services import env_inject

# ---- Test 1: success path, var was unset --------------------------------- #


def test_scope_sets_and_unsets_previously_unset_var(monkeypatch):
    monkeypatch.delenv("FOO_ENV_INJECT_TEST", raising=False)
    assert "FOO_ENV_INJECT_TEST" not in os.environ

    with env_inject.scope({"FOO_ENV_INJECT_TEST": "bar"}):
        assert os.environ["FOO_ENV_INJECT_TEST"] == "bar"

    assert "FOO_ENV_INJECT_TEST" not in os.environ


# ---- Test 2: existing var restored --------------------------------------- #


def test_scope_restores_existing_var(monkeypatch):
    monkeypatch.setenv("BAZ_ENV_INJECT_TEST", "original")

    with env_inject.scope({"BAZ_ENV_INJECT_TEST": "override"}):
        assert os.environ["BAZ_ENV_INJECT_TEST"] == "override"

    assert os.environ["BAZ_ENV_INJECT_TEST"] == "original"


# ---- Test 3: exception path ---------------------------------------------- #


def test_scope_restores_env_on_exception(monkeypatch):
    monkeypatch.delenv("EXC_ENV_INJECT_TEST", raising=False)
    monkeypatch.setenv("EXC_PREEXISTING", "keep-me")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), env_inject.scope(
        {"EXC_ENV_INJECT_TEST": "transient", "EXC_PREEXISTING": "stomped"}
    ):
        assert os.environ["EXC_ENV_INJECT_TEST"] == "transient"
        assert os.environ["EXC_PREEXISTING"] == "stomped"
        raise Boom("kaboom")

    # Both vars restored to their pre-scope state despite the exception.
    assert "EXC_ENV_INJECT_TEST" not in os.environ
    assert os.environ["EXC_PREEXISTING"] == "keep-me"


# ---- Test 4: asyncio.CancelledError path --------------------------------- #


def test_scope_restores_env_on_cancelled_error(monkeypatch):
    """The plan specifically requires restoration on ``CancelledError``.

    ``CancelledError`` is a ``BaseException`` (not an ``Exception``), so
    a bare ``except Exception:`` would miss it. The contextmanager uses
    a ``finally:`` block which catches both, but this test guards the
    invariant.

    We drive the coroutine via a fresh, locally-scoped event loop (and
    restore the original via ``asyncio.set_event_loop`` in ``finally``)
    rather than ``asyncio.run`` because ``asyncio.run`` permanently
    detaches the main-thread event loop on Windows, which breaks the
    sync ``TestClient`` used by the rate-limit tests later in the
    session.
    """
    monkeypatch.delenv("CANCEL_ENV_INJECT_TEST", raising=False)

    async def _runner():
        with env_inject.scope({"CANCEL_ENV_INJECT_TEST": "in-flight"}):
            assert os.environ["CANCEL_ENV_INJECT_TEST"] == "in-flight"
            raise asyncio.CancelledError()

    try:
        previous_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous_loop = None

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(_runner())
    finally:
        loop.close()
        asyncio.set_event_loop(previous_loop)

    assert "CANCEL_ENV_INJECT_TEST" not in os.environ


# ---- Test 5: multiple keys, mixed pre-existence -------------------------- #


def test_scope_restores_mixed_preexisting_and_new_keys(monkeypatch):
    monkeypatch.setenv("MIX_EXISTING_A", "alpha-original")
    monkeypatch.setenv("MIX_EXISTING_B", "beta-original")
    monkeypatch.delenv("MIX_NEW_C", raising=False)
    monkeypatch.delenv("MIX_NEW_D", raising=False)

    payload = {
        "MIX_EXISTING_A": "alpha-override",
        "MIX_EXISTING_B": "beta-override",
        "MIX_NEW_C": "gamma-new",
        "MIX_NEW_D": "delta-new",
    }

    with env_inject.scope(payload):
        assert os.environ["MIX_EXISTING_A"] == "alpha-override"
        assert os.environ["MIX_EXISTING_B"] == "beta-override"
        assert os.environ["MIX_NEW_C"] == "gamma-new"
        assert os.environ["MIX_NEW_D"] == "delta-new"

    # Pre-existing restored to original values; new vars removed.
    assert os.environ["MIX_EXISTING_A"] == "alpha-original"
    assert os.environ["MIX_EXISTING_B"] == "beta-original"
    assert "MIX_NEW_C" not in os.environ
    assert "MIX_NEW_D" not in os.environ


# ---- Test 6: empty dict is a no-op --------------------------------------- #


def test_scope_with_empty_mapping_is_noop(monkeypatch):
    monkeypatch.setenv("NOOP_SENTINEL", "untouched")
    snapshot_keys = set(os.environ.keys())

    with env_inject.scope({}):
        # Nothing added.
        assert set(os.environ.keys()) == snapshot_keys
        assert os.environ["NOOP_SENTINEL"] == "untouched"

    assert set(os.environ.keys()) == snapshot_keys
    assert os.environ["NOOP_SENTINEL"] == "untouched"
