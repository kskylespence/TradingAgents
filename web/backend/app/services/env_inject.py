"""Snapshot/restore ``os.environ`` for a single run.

The runner needs to make decrypted provider API keys visible to the
``tradingagents`` package via ``os.getenv(...)`` (that's how every LLM
client and dataflow lookup resolves a key today). The cheapest way is to
mutate ``os.environ`` for the duration of the run and restore the prior
state on exit.

Lock invariant
--------------
This module performs **process-global** mutation of ``os.environ``. It is
therefore only safe when the caller holds the ``GLOBAL_RUN_LOCK``
``asyncio.Lock`` defined in :mod:`app.services.run_service` — that lock
guarantees v1 runs at most one analysis at a time, so no concurrent run
can observe or stomp the injected keys.

For **v2 (concurrent runs)**: do not extend this module. Instead, plumb
``api_key=...`` per call through ``create_llm_client(...)`` so each
in-flight run carries its own credential without touching process state.

Restoration guarantees
----------------------
The ``finally`` block restores the prior environment on:

- normal exit
- arbitrary ``Exception`` raised inside the ``with`` block
- ``asyncio.CancelledError`` raised inside the ``with`` block (e.g. when
  the run task is cancelled)

Variables that were unset before entering the scope are *removed* on
exit (not left as the empty string), so ``os.getenv(name)`` returns
``None`` again — matching the pre-scope observation.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Mapping


@contextmanager
def scope(api_keys: Mapping[str, str]) -> Iterator[None]:
    """Inject ``api_keys`` into ``os.environ`` for the duration of the block.

    Parameters
    ----------
    api_keys
        Mapping of env-var name to value. Empty mapping is a no-op.

    Yields
    ------
    None
        Control is yielded to the ``with`` body while the keys are
        applied; the original environment is restored when the body
        exits for any reason (success, exception, cancellation).

    Notes
    -----
    Caller MUST hold ``GLOBAL_RUN_LOCK`` (see module docstring) — the
    function does not acquire any lock of its own.
    """
    saved = {k: os.environ.get(k) for k in api_keys}
    os.environ.update(api_keys)
    try:
        yield
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original
