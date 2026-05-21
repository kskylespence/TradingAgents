"""Shared asset-type detection and analyst filtering.

Used by both the CLI and the web backend so ticker classification and the
crypto-specific analyst filter stay in sync. `AssetType` and `AnalystType`
enums remain in `cli/models.py` (already a shared-friendly location).
"""

from __future__ import annotations

from typing import List

from cli.models import AnalystType, AssetType

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def detect_asset_type(ticker: str) -> AssetType:
    normalized_ticker = ticker.strip().upper()
    if normalized_ticker.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    return AssetType.STOCK


def filter_analysts_for_asset_type(
    analysts: List[AnalystType], asset_type: AssetType
) -> List[AnalystType]:
    if asset_type != AssetType.CRYPTO:
        return analysts
    return [
        analyst
        for analyst in analysts
        if analyst != AnalystType.FUNDAMENTALS
    ]
