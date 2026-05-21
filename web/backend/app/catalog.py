"""Catalog wrapper: translates tradingagents sources into Pydantic shapes.

The frontend form (NewRun.tsx) is entirely driven by /api/catalog/*. This
module is the only place the web backend reaches into the parent package
to read the provider list, model catalog, analyst registry, and the
hardcoded language list.

Source modules (DO NOT duplicate the data here — re-read on import is fine,
these are tiny module-level constants):

- ``tradingagents.providers.PROVIDERS`` — list of ``ProviderSpec``.
- ``tradingagents.llm_clients.model_catalog.MODEL_OPTIONS`` — per-provider,
  per-mode model lists. Entries with ``id == "custom"`` mean the provider
  accepts an arbitrary model ID typed in by the user; we surface that as
  a synthetic terminal entry with ``id="__custom__"`` so the frontend can
  swap the dropdown for a text input.
- ``tradingagents.llm_clients.api_key_env.PROVIDER_API_KEY_ENV`` — env var
  per provider. Ollama maps to ``None`` because the local runtime has no
  authentication.
- ``cli.models.AnalystType`` + ``tradingagents.asset_types`` — analyst
  enum + crypto-aware filter. The wire key ``"social"`` displays as
  ``"Sentiment Analyst"`` (per CLAUDE.md — do not rename the wire key).
- The 11 hardcoded languages come from ``cli/utils.py:ask_output_language``
  (Questionary choices list, with a 12th "custom" sentinel we drop). The
  cli list is the source of truth; we inline the values here because the
  CLI keeps them as Questionary ``Choice`` objects rather than a plain
  constant — extracting that for sharing is out of scope for this team.
"""

from __future__ import annotations

from typing import Literal

from cli.models import AnalystType
from tradingagents.asset_types import filter_analysts_for_asset_type
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from tradingagents.providers import available_providers

from .schemas import (
    AssetType,
    CatalogAnalyst,
    CatalogLanguage,
    CatalogModel,
    CatalogProvider,
)

Mode = Literal["quick", "deep"]


# ---------------------------------------------------------------------------
# Analysts
# ---------------------------------------------------------------------------

# Display labels for the 4 analyst wire keys. ``social`` -> Sentiment Analyst
# is enforced by CLAUDE.md: the wire key is NOT renamed; only the label
# changes.
ANALYST_LABELS: dict[str, str] = {
    AnalystType.MARKET.value: "Market Analyst",
    AnalystType.SOCIAL.value: "Sentiment Analyst",
    AnalystType.NEWS.value: "News Analyst",
    AnalystType.FUNDAMENTALS.value: "Fundamentals Analyst",
}


# ---------------------------------------------------------------------------
# Languages (mirrors cli/utils.py:ask_output_language Questionary choices)
# ---------------------------------------------------------------------------
#
# Source of truth: cli/utils.py:499-516. That list also includes a "custom"
# 12th entry that lets the CLI accept free-form text. The web form handles
# free-form language via a plain text input outside the catalog, so we
# only enumerate the 11 named languages here.
_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("English", "English (default)"),
    ("Chinese", "Chinese (中文)"),
    ("Japanese", "Japanese (日本語)"),
    ("Korean", "Korean (한국어)"),
    ("Hindi", "Hindi (हिन्दी)"),
    ("Spanish", "Spanish (Español)"),
    ("Portuguese", "Portuguese (Português)"),
    ("French", "French (Français)"),
    ("German", "German (Deutsch)"),
    ("Arabic", "Arabic (العربية)"),
    ("Russian", "Russian (Русский)"),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_providers() -> list[CatalogProvider]:
    """Translate the env-filtered provider list into ``CatalogProvider`` instances.

    Only providers whose credentials are present in the environment are
    returned — see ``tradingagents.providers.available_providers``. This
    makes the frontend dropdown honest: a user can't pick a provider that
    we have no way to talk to.

    ``regions`` is intentionally ``None`` for now — the underlying
    ``ProviderSpec`` dataclass does not expose a region grouping.

    ``requires_api_key`` is ``False`` only for ollama. ``api_key_env``
    falls back to an empty string for ollama since the dataclass requires
    a str — the frontend keys off ``requires_api_key`` for whether to
    prompt at all.
    """
    out: list[CatalogProvider] = []
    for spec in available_providers():
        env_var = PROVIDER_API_KEY_ENV.get(spec.key)
        requires = env_var is not None
        out.append(
            CatalogProvider(
                key=spec.key,
                label=spec.display,
                regions=None,
                requires_api_key=requires,
                api_key_env=env_var or "",
            )
        )
    return out


async def list_models(provider: str, mode: Mode) -> list[CatalogModel]:
    """Return model options for ``(provider, mode)``.

    Ollama is **live-discovered** via ``app.services.ollama_models``:
    we call ``GET {OLLAMA_BASE_URL}/models`` (the OpenAI-compatible list
    endpoint, supported by both local ollama-serve and Ollama Cloud) and
    return whatever IDs the upstream reports. No ``__custom__`` sentinel
    for ollama — the user picks from what's discoverably callable.

    Other providers come from the static ``MODEL_OPTIONS``. Those whose
    catalog includes a ``"custom"`` entry (deepseek, qwen, glm, minimax,
    plus their -cn variants) get a synthetic ``__custom__`` terminal
    entry so the frontend can swap to a text input. OpenRouter and Azure
    aren't in ``MODEL_OPTIONS``; they fall back to the custom-only entry.
    """
    provider_key = provider.lower()

    # Ollama: live discovery, never a __custom__ sentinel.
    if provider_key == "ollama":
        from app.services.ollama_models import list_ollama_models

        ids = await list_ollama_models()
        return [
            CatalogModel(id=mid, label=mid, allows_custom=False)
            for mid in ids
        ]

    mode_options = MODEL_OPTIONS.get(provider_key, {})
    entries = mode_options.get(mode, [])

    out: list[CatalogModel] = []
    has_custom = False
    for label, value in entries:
        if value == "custom":
            has_custom = True
            continue
        out.append(CatalogModel(id=value, label=label, allows_custom=False))

    # Providers without static entries (openrouter, azure) still need a
    # custom-model affordance; treat them like the explicit-custom group.
    if not entries:
        has_custom = True

    if has_custom:
        out.append(
            CatalogModel(
                id="__custom__",
                label="Custom model ID",
                allows_custom=True,
            )
        )
    return out


def list_analysts(asset_type: str) -> list[CatalogAnalyst]:
    """Return analyst options, filtered for the given asset type.

    Crypto excludes Fundamentals (per
    ``tradingagents.asset_types.filter_analysts_for_asset_type``). Stock
    keeps all four. The wire key ``"social"`` displays as
    ``"Sentiment Analyst"`` — do NOT rename the wire key (CLAUDE.md).
    """
    # Coerce to the AssetType enum that filter_analysts_for_asset_type
    # expects. ``cli.models.AssetType`` has values "stock" / "crypto"
    # matching the Literal in app/schemas.py.
    from cli.models import AssetType as AssetTypeEnum

    if asset_type == "crypto":
        asset_enum = AssetTypeEnum.CRYPTO
    else:
        asset_enum = AssetTypeEnum.STOCK

    all_analysts = list(AnalystType)
    filtered = filter_analysts_for_asset_type(all_analysts, asset_enum)
    return [
        CatalogAnalyst(key=a.value, label=ANALYST_LABELS[a.value])
        for a in filtered
    ]


def list_languages() -> list[CatalogLanguage]:
    """Return the 11 hardcoded output languages."""
    return [CatalogLanguage(key=key, label=label) for key, label in _LANGUAGES]


__all__ = [
    "list_providers",
    "list_models",
    "list_analysts",
    "list_languages",
    "Mode",
]
