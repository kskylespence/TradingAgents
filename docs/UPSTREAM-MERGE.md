# Upstream merges

Latest: [`TauricResearch/TradingAgents`](https://github.com/TauricResearch/TradingAgents)
**v0.6.0** (2026-10-03) → fork **`0.6.0+hf.1`**. Earlier: v0.5.1 → `0.5.1+hf.1`
(2026-09-24), v0.3.1 → `0.3.1+hf.1` (2026-07-08).

## Current state

| | Fork | Upstream |
|---|------|----------|
| Version | `0.6.0+hf.1` (web backend; the package itself reports `0.6.0`) | `v0.6.0` (2026-10-03) |
| Python | 3.11+ (image runs 3.12) | 3.11+ (image runs 3.13) |
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
2. `git fetch upstream --tags`, then squash-merge the tag and merge it
   `--no-ff` afterwards (RELEASING.md §2.2). The second merge records the
   ancestry; with the squash alone git still thinks the base is the old
   release and every later sync re-diffs from there. Do it on a branch.
3. Resolve conflicts prioritizing fork-owned paths:
   - `web/backend/`, `web/frontend/`, `DEPLOY.md`, `CHANGELOG.fork.md`
4. Run the whole local CI pass, RELEASING.md §4 (root suite on the CI Python
   versions, web backend, frontend, ruff, `pip-audit`, the Docker build).
   Nothing is pushed until it is green.
5. Set the web version to `<new upstream base>+hf.1` in the sync itself
   (`tests/test_fork_version.py` enforces it), then cut per RELEASING.md §1 —
   reset the counter only when the upstream *base* version moves
   (post-v0.3.1 commits do not reset it).

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

**v0.5.1 sync: 2026-09-24.** Merged the signed tag `v0.5.1` (verified via
the GitHub API; commit `35543d0`), 136 commits past `a33fd4c`, on branch
`chore/security-refresh-2026-09`. Upstream reorganised modules, which the
fork's web backend imports; adaptations a future sync should expect:

- `safe_ticker_component` moved to `tradingagents/dataflows/symbols.py`,
  `parse_rating` to `tradingagents/agents/rating.py` (default is now
  `REVIEW`; the web passes `default="Hold"`).
- `AnalystWallTimeTracker` / `sync_analyst_tracker_from_chunk` moved to
  `cli/display.py`; the fork re-adds `get_wall_times()` there (upstream
  deleted it as unused). The web runner imports from `cli.display`.
- `cli/main.py` became a thin Typer app (`display`, `prompts`,
  `selections`, `run`); `cli/prompts.py` imports the shared
  `tradingagents.asset_types` / `providers` helpers.
- `graph/trading_graph.py`: upstream's `begin_checkpoint` /
  `checkpoint_input` / `end_checkpoint` / `create_run_state` /
  `record_decision` now drive the web engine too (resume + decision log).
- `model_catalog.py`: the fork still lists no static Ollama models.

Grep-based checks miss lazy imports; after a sync, import every module
(`pkgutil.walk_packages` over `tradingagents`, `cli`, `app`) and run
`web/backend/tests/test_run_engine_real_graph.py`, which drives the real
engine offline — FAKE_LLM tests skip it entirely.

Verified after merge: root 1073 passed (2 did not run: optional Bedrock
extra, live DeepSeek), web backend 268 passed, ruff clean.

**v0.6.0 sync: 2026-10-12.** Squash-merged the tag `v0.6.0` (a lightweight tag;
commit `1394a3f`, which also covers v0.5.2), 89 commits past `v0.5.1`, on branch
`chore/upstream-v0.6.0`. Only 13 files changed on both sides, 8 conflicted.
Adaptations a future sync should expect:

- **Analysts run at the same time**, each as its own subgraph
  (`graph/setup.py:_analyst_graph`), and a `Memory Log` node settles past
  decisions beside them. `AnalystNodeSpec` has no `tool_node` / `clear_node`
  any more. The fork's `_tag_agent` wraps the analyst function before it goes
  into the subgraph; the retry policy is stamped on the top-level nodes, so a
  transient provider error re-runs the whole analyst, not one step.
- **The heartbeat label is a `ContextVar`**, not an attribute of the client
  (`llm_clients/openai_client.py:_heartbeat_agent`). All analysts share one
  quick-model client now, so a label stored on it names whichever analyst
  tagged last. Never put per-node state on the shared client.
- `tradingagents/decision_log.py` became the `tradingagents.memory` package
  (`log`, `settlement`, `reflection`); `graph/settlement.py` and
  `graph/reflection.py` are gone, and `get_closes` lives in
  `dataflows/vendors/yahoo/market.py`. `web/backend/tests/test_run_engine_real_graph.py`
  imported the old paths and failed at collection.
- `graph/trading_graph.py` builds clients with `create_tier_client(config,
  "quick" | "deep")` (a provider per tier) instead of `create_llm_client`; the
  engine tests patch that name now.
- **Python 3.10 is dropped** (`requires-python >=3.11`, ruff `py311`). The CI
  matrix is 3.11–3.14 and ruff's `UP017` / `UP041` now apply, which touched
  about 25 files under `web/backend/`.
- **The package version has one source**, `tradingagents.__version__`, and
  upstream's `tests/test_version.py` rejects `+hf.N` there. The fork counter
  moved to `web/backend/app/__init__.py` only (RELEASING.md).
- v0.5.2 → v0.6.0 checkpoints are not compatible: a run checkpointed before the
  deploy starts over after it.
- Upstream's `docker-compose.yml` and `Dockerfile` are the CLI image; the fork's
  are the web stack, so both were kept as ours.

Two fork tests had hidden problems the sync exposed. `tests/test_llm_call_heartbeat.py`,
`test_ollama_api_key.py`, `test_openai_client_retry.py` and `test_per_model_timeout.py`
called `importlib.reload(openai_client)`, which rebinds the module's classes, so
upstream's `test_openai_compatible_provider.py` (which imports them at the top)
failed only in the full run. They use the live module now; do not reload it.
Upstream's `tests/test_cache_writes.py` used `errno.EDEADLOCK`, which macOS does
not define; `dataflows/files.py` has a `_EDEADLOCK` constant now.

Verified after merge, all on this machine: root 1339 passed in the main venv
and 1327 passed (2 skipped for named reasons, see RELEASING.md §4) in fresh
py3.11, 3.12, 3.12 + `TZ=America/New_York` and 3.14 environments; web backend
291 passed; frontend 77 passed, build clean, `npm audit` clean; ruff clean;
`pip-audit` on `requirements.lock` clean; the Docker image built from the lock;
all 137 modules of `tradingagents`, `cli` and `app` import.
