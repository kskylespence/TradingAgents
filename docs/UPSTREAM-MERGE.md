# Upstream merge schedule (v0.3.1)

This document tracks the planned merge of
[`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents)
**v0.3.1** into the fork. It is **not** an emergency CPU fix — upstream
v0.3.x improves correctness and data contracts but does not ship parallel
analyst execution or major resource reductions.

## Current state

| | Fork | Upstream |
|---|------|----------|
| Version | `0.2.5+hf.4` | `v0.3.1` (2026-07-05) |
| Web UI / Coolify | Yes | No |
| Resilience (run timeout, Ollama circuit breaker) | Yes | Partial (`TRADINGAGENTS_LLM_MAX_RETRIES` in v0.3.1) |

## Worth taking from v0.3.x

- Debate/risk router crash fix (#1088) — shared path maps on every edge
- Checkpoint thread id respects analyst + depth selection (#1089)
- Verified data-access contract (stale OHLCV rejection, look-ahead-safe news)
- `openai_compatible` generic endpoint for vLLM / LM Studio relays
- Configurable LLM retry budget (already partially in fork web layer)

## Defer / evaluate carefully

- New data vendors (FRED, Polymarket) — more network fetches per run
- Provider registry refactor — large touch surface vs fork `web/backend`
- Removed `analyst_concurrency_limit` — was already a no-op in fork

## Recommended merge workflow

Follow [`docs/RELEASING.md`](RELEASING.md) §2 (upstream merge) when ready:

1. Stabilize VPS with [lite preset](../web/docs/operations.md#lite-vps-preset)
   and [GHCR prebuilt deploy](../DEPLOY.md#prebuilt-image-ghcr).
2. `git fetch upstream && git merge upstream/main` on a dedicated branch.
3. Resolve conflicts prioritizing fork-owned paths:
   - `web/backend/`, `web/frontend/`, `DEPLOY.md`, `CHANGELOG.fork.md`
4. Run full test matrix: `pytest`, `web/backend` pytest, frontend vitest.
5. Cut `0.3.0+hf.1` (or next base) per RELEASING.md — reset `+hf.` counter.

## Status

**Scheduled — not started.** Open a dedicated session for the merge; do not
combine with VPS ops or resilience tweaks.
