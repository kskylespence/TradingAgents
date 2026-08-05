"""Snapshot of Ollama's actively-curated cloud model catalog.

What this is
============
``CURATED_2026_08`` is a frozen membership set of model **base names**
that appeared in Ollama's official curated cloud catalog at
https://ollama.com/search?c=cloud on 2026-08-05. The catalog endpoint
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

* **Cadence:** quarterly review. The next due date is 2026-11-05.
* **Triggered:** also bump immediately when a user reports a new
  curated model isn't being prioritised, or when Ollama publishes a new
  base model.
* **Process:** load https://ollama.com/search?c=cloud, copy the **base
  names** into ``CURATED_2026_08``, rename the constant to the new date,
  and bump the snapshot date in the docstring above. Do NOT paste tagged
  IDs from ``/v1/models`` — ``is_curated`` strips the tag before the
  lookup, so a tagged entry is dead weight that can never match. The
  tests in ``tests/test_catalog_curated_flag.py`` lock the contract
  (``kimi-k2-thinking`` stays not-curated, tagged variants of a curated
  base resolve to True), so a refresh that drops a known-good model
  fails loudly.

  Expect the guard to fire on genuine upstream retirements too — the
  2026-08-05 refresh tripped it because ``glm-5`` had been superseded by
  5.1/5.2 and dropped from the catalog. That is the test doing its job,
  not a regression; retire the assertion along with the model.

Why a frozenset
===============
Membership checks are O(1) and the value is immutable for the lifetime
of the process — there's no scenario where curated status should flip
mid-request. Mutability would also defeat the snapshot semantics.
"""

from __future__ import annotations

#: Snapshot of https://ollama.com/search?c=cloud taken 2026-08-05.
#:
#: **Base names only — never tags.** ``is_curated`` strips the ``:tag``
#: before the lookup, so ``deepseek-v4-flash`` covers ``:0731`` and
#: ``:preview`` alike. Adding a tagged entry here is harmless but dead
#: weight; it can never match.
#:
#: Order is irrelevant (set semantics) but kept roughly grouped by
#: model family for readability when reviewing the next refresh diff.
CURATED_2026_08: frozenset[str] = frozenset(
    {
        # Z.AI GLM family
        "glm-5.2",
        "glm-5.1",
        # Moonshot Kimi family
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        # Alibaba Qwen family
        "qwen3.5",
        # DeepSeek family
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        # OpenAI OSS family
        "gpt-oss",
        # NVIDIA Nemotron family
        "nemotron-3-ultra",
        "nemotron-3-super",
        "nemotron-3-nano",
        # MiniMax family
        "minimax-m3",
        "minimax-m2.7",
        # Google Gemma
        "gemma4",
        # Mistral family
        "mistral-large-3",
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

    Matching is on the **base name** — everything before the first ``:``.
    The upstream search page lists bases (``deepseek-v4-flash``) while
    ``/v1/models`` returns tags (``deepseek-v4-flash:0731``), so an exact
    match badged models that were curated all along. Ignoring the tag also
    means a routine tag rotation (``:0731`` → ``:0801``) can't make a
    known-good model start showing a reliability warning it never earned.

    A tag cannot launder a non-curated model: ``qwen3-coder:480b`` reduces
    to ``qwen3-coder``, which is absent from the snapshot and stays False.
    """
    if not model_id:
        return False
    base = model_id.split(":", 1)[0]
    if not base:
        # ``":latest"`` and friends — an empty base must not be treated as
        # a match against anything.
        return False
    return base in CURATED_2026_08


__all__ = ["CURATED_2026_08", "is_curated"]
