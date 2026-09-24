"""Regular users' runs get the admin-default models, falling back to the code defaults.

``apply_admin_defaults_for_user`` overwrites whatever model a non-admin sent.
Two fallback paths decide what they get when the admin hasn't picked one:

* no ``user_defaults`` row at all → the ``UserDefaults`` schema defaults;
* a row whose model columns are NULL → the ``or``-fallbacks in run_access.

Both must name the same models, or a regular user's run silently depends on
whether the admin ever opened the Settings page.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.models import UserDefaults as UserDefaultsModel
from app.schemas import RunRequest
from app.services.run_access import apply_admin_defaults_for_user


class _FakeSession:
    """Stands in for ``AsyncSession``; only ``get`` is used by the helper."""

    def __init__(self, row: UserDefaultsModel | None) -> None:
        self._row = row

    async def get(self, _model, _pk):
        return self._row


def _user_request() -> RunRequest:
    return RunRequest(
        ticker="NVDA",
        analysis_date=date(2026, 9, 24),
        output_language="English",
        analysts=["market"],
        research_depth=1,
        llm_provider="ollama",
        quick_think_llm="something-the-user-picked",
        deep_think_llm="something-else",
    )


@pytest.mark.asyncio
async def test_no_admin_row_uses_glm_5_3_defaults() -> None:
    out = await apply_admin_defaults_for_user(_user_request(), _FakeSession(None))
    assert out.llm_provider == "ollama"
    assert out.quick_think_llm == "glm-5.3-flash"
    assert out.deep_think_llm == "glm-5.3"


@pytest.mark.asyncio
async def test_admin_row_with_null_models_uses_glm_5_3_defaults() -> None:
    row = UserDefaultsModel(
        id=1, llm_provider=None, quick_think_llm=None, deep_think_llm=None,
        enable_checkpoint=True,
    )
    out = await apply_admin_defaults_for_user(_user_request(), _FakeSession(row))
    assert out.llm_provider == "ollama"
    assert out.quick_think_llm == "glm-5.3-flash"
    assert out.deep_think_llm == "glm-5.3"


@pytest.mark.asyncio
async def test_admin_row_models_win_over_defaults() -> None:
    row = UserDefaultsModel(
        id=1, llm_provider="ollama", quick_think_llm="kimi-k2.6", deep_think_llm="glm-5.2",
        enable_checkpoint=True,
    )
    out = await apply_admin_defaults_for_user(_user_request(), _FakeSession(row))
    assert out.quick_think_llm == "kimi-k2.6"
    assert out.deep_think_llm == "glm-5.2"
