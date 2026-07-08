"""Snapshot of Ollama's actively-curated cloud model catalog.

What this is
============
``CURATED_2026_05`` is a frozen membership set of model IDs that
appeared in Ollama's official curated cloud catalog at
https://ollama.com/search?c=cloud on 2026-05-23. The catalog endpoint
flags every Ollama model with ``curated: bool`` derived from this set
so the frontend can sort safer-known options first and badge the rest.

Why this exists
===============
Ollama Cloud's ``/v1/models`` endpoint advertises every model an account
has access to, including models that Ollama has quietly de-emphasised
in its curated catalog. Two of those models have publicly tracked
reliability issues:

* ``kimi-k2-thinking`` — ollama/ollama#15453 documents a ~95% failure
  rate on long-context runs.
* ``qwen3-coder:480b`` — ollama/ollama#14542 documents tool-call 500s.

Hiding them entirely would surprise power users who picked them
deliberately. The middle path — keep them in the dropdown but de-rank
and badge — preserves choice while steering new users toward the
curated set.

Refresh policy
==============
The snapshot is a point-in-time copy, not a live mirror. Ollama doesn't
publish a stable manifest of the curated set and we don't want a
runtime dependency on scraping their search page. Refresh policy:

* **Cadence:** quarterly review. The next due date is 2026-08-23.
* **Triggered:** also bump immediately when a user reports a new
  curated model isn't being prioritised, or when Ollama publishes a new
  base model.
* **Process:** load https://ollama.com/search?c=cloud, copy the model
  IDs verbatim into ``CURATED_2026_05``, rename the constant to the
  new date, and bump the snapshot date in the docstring above. The
  tests in ``tests/test_catalog_curated_flag.py`` lock the contract
  (specifically that ``glm-5`` stays curated and ``kimi-k2-thinking``
  stays not-curated), so a refresh that drops a known-good model will
  fail loudly.

Why a frozenset
===============
Membership checks are O(1) and the value is immutable for the lifetime
of the process — there's no scenario where curated status should flip
mid-request. Mutability would also defeat the snapshot semantics.
"""

from __future__ import annotations

#: Snapshot of https://ollama.com/search?c=cloud taken 2026-05-23.
#:
#: Order is irrelevant (set semantics) but kept roughly grouped by
#: model family for readability when reviewing the next refresh diff.
CURATED_2026_05: frozenset[str] = frozenset(
    {
        # Z.AI GLM family
        "glm-5.2",
        "glm-5",
        "glm-5.1",
        # Moonshot Kimi family
        "kimi-k2.6",
        "kimi-k2.5",
        "kimi-k2:1t",
        # Alibaba Qwen family
        "qwen3-next:80b",
        "qwen3-coder-next",
        "qwen3.5:397b",
        "qwen3-vl:235b",
        "qwen3-vl:235b-instruct",
        # DeepSeek family
        "deepseek-v3.2",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        # OpenAI OSS family
        "gpt-oss:120b",
        "gpt-oss:20b",
        # NVIDIA Nemotron family
        "nemotron-3-super",
        "nemotron-3-nano:30b",
        # MiniMax family
        "minimax-m2",
        "minimax-m2.1",
        "minimax-m2.5",
        "minimax-m2.7",
        # Google Gemini
        "gemini-3-flash-preview",
        # Mistral family
        "devstral-2:123b",
        "devstral-small-2:24b",
        "mistral-large-3:675b",
        "ministral-3:8b",
        "ministral-3:14b",
        # Cogito family
        "cogito-2.1:671b",
    }
)


def is_curated(model_id: str) -> bool:
    """Return True iff ``model_id`` is in the active curated cloud catalog.

    Unknown models — including new ones the snapshot hasn't seen yet —
    return ``False``. That's the conservative default: a never-seen
    model is treated like a deprioritised one and shown with the
    warning badge in the UI. The cost of mislabelling a good model as
    non-curated is a small UI badge; the cost of the inverse (marking
    ``kimi-k2-thinking`` curated and removing the warning) is a
    frustrated user. Bias toward warning.

    Empty / falsy IDs also return False — they shouldn't occur (the
    upstream client filters non-string IDs), but defensive zero-value
    handling keeps this safe to call from any context.
    """
    if not model_id:
        return False
    return model_id in CURATED_2026_05


__all__ = ["CURATED_2026_05", "is_curated"]
