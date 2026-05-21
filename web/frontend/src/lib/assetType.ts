import type { AssetType } from "@/lib/types";

/**
 * Client-side asset-type derivation. Mirrors the backend's
 * `tradingagents.asset_types.detect_asset_type` (CRYPTO_SUFFIXES). Kept in
 * sync intentionally so we can filter the analyst catalog (which is
 * keyed by asset type) without an extra round-trip on every keystroke.
 *
 * The backend remains the source of truth at submit time — `/api/runs`
 * re-derives the asset type from the ticker, so a stale client-side
 * heuristic can't sneak past validation.
 */
const CRYPTO_SUFFIXES = ["-USD", "-USDT", "-USDC", "-BTC", "-ETH"] as const;

export function inferAssetType(ticker: string): AssetType {
  const normalized = ticker.trim().toUpperCase();
  if (!normalized) return "stock";
  for (const suffix of CRYPTO_SUFFIXES) {
    if (normalized.endsWith(suffix)) return "crypto";
  }
  return "stock";
}
