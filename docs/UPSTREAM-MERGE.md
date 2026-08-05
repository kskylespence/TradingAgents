# Upstream merge — v0.3.1 (completed 2026-07-08)

Merged [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents)
**v0.3.1** on branch `merge/upstream-v0.3.1` → fork **`0.3.1+hf.1`**.

## Current state

| | Fork | Upstream |
|---|------|----------|
| Version | `0.3.1+hf.1` | `v0.3.1` (2026-07-05) |
| Web UI / Coolify | Yes | No |
| Resilience (run timeout, Ollama circuit breaker) | Yes | Partial (`TRADINGAGENTS_LLM_MAX_RETRIES` in v0.3.1) |

## Integrated from v0.3.x

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
2. `git fetch upstream && git merge upstream/main`. Merge onto `main` —
   this repo is worked single-branch, so the "dedicated branch" step from
   earlier revisions of this doc no longer applies.
3. Resolve conflicts prioritizing fork-owned paths:
   - `web/backend/`, `web/frontend/`, `DEPLOY.md`, `CHANGELOG.fork.md`
4. Run full test matrix: `pytest`, `web/backend` pytest, frontend vitest.
5. Cut the next `+hf.` per RELEASING.md — reset the counter only when the
   upstream *base* version moves (post-v0.3.1 commits do not reset it).

**Re-fetch `upstream/main` before judging whether you are current.** The
remote-tracking ref is only as fresh as your last `git fetch upstream`; a
stale one shows the fork level with upstream when it is not. The
2026-08-05 sync below was six commits behind a ref last fetched 2026-07-08.

## Status

**v0.3.1 merge: complete (2026-07-08).**

**Post-v0.3.1 sync: complete (2026-08-05).** Merged `upstream/main` at
`a33fd4c` (`v0.3.1-6-ga33fd4c`) directly into `main`, conflict-free —
`README.md` and `cli/main.py` were touched on both sides and auto-merged.
Brought in four fixes, two of which matter to this fork's data path:

- `40774ca` Yahoo news window is now UTC and end-exclusive — an article
  stamped exactly midnight after `end_date` used to leak into a historical
  run, and flat epoch stamps were parsed in host-local time, making results
  machine-dependent (look-ahead bias, #1126).
- `d78c698` same-day OHLCV cache is TTL-governed — the per-day cache was
  reused unconditionally, feeding a partial intraday candle into technical
  analysis as a stale close (#1150).
- `030b434` schema-only structured agents no longer prime tool calls — the
  primed model emitted an unknown `web_search` call, so the attempt was
  discarded for a free-text retry, costing a round trip and the typed
  output (#1130).
- `3f6c082` CLI reports an unusable terminal instead of a raw
  `NoConsoleScreenBufferError` traceback (Windows-only, #1138).

Verified after merge: root `pytest -m "not integration"` 644 passed
(up from 627 — upstream added three test files), `web/backend` pytest
239 passed, frontend `npm run build` + 61 vitest passing, `ruff` clean.
