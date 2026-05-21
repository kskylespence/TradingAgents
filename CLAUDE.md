# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

TradingAgents (package name `tradingagents`, local repo folder `HedgeFund`) is a LangGraph-based multi-agent LLM trading framework. Specialized agents (Analysts → Researchers → Research Manager → Trader → Risk Debaters → Portfolio Manager) collaborate to produce a Buy/Overweight/Hold/Underweight/Sell recommendation for a ticker on a given date. It is a research framework — not financial advice and not a backtester runtime.

## Common commands

```bash
# Install (editable is implicit when running tests off the source tree)
pip install .

# Launch the interactive CLI (Typer + Rich + Questionary)
tradingagents analyze                       # installed console script
python -m cli.main analyze                  # equivalent
python main.py                              # programmatic one-shot (NVDA, hardcoded date)

# CLI options
tradingagents analyze --checkpoint          # opt into LangGraph SQLite checkpoint/resume
tradingagents analyze --clear-checkpoints   # wipe all checkpoint DBs before running

# Tests
pytest                                      # full suite (pyproject.toml sets testpaths=tests)
pytest tests/test_capabilities.py           # one file
pytest tests/test_capabilities.py::test_minimax_skips_tool_choice  # one test
pytest -m unit                              # only unit-marked
pytest -m "not integration"                 # skip integration

# Docker
docker compose run --rm tradingagents
docker compose --profile ollama run --rm tradingagents-ollama
```

`tests/conftest.py` injects placeholder API keys for every supported provider via an autouse fixture, so the suite never hangs on a missing key. Real network calls in unit tests should be mocked — `mock_llm_client` fixture is provided.

## High-level architecture

### Pipeline (LangGraph `StateGraph`)

```
START → Analyst 1 ⇄ tools → … → Analyst N ⇄ tools
       → Bull Researcher ⇄ Bear Researcher → Research Manager
       → Trader
       → Aggressive ⇄ Conservative ⇄ Neutral (risk debate)
       → Portfolio Manager → END
```

Analysts are run **sequentially**, each looping with its own `ToolNode` until the conditional logic decides it's done, then a `create_msg_delete` clear node strips messages before handing off to the next analyst. Wiring lives in `tradingagents/graph/setup.py:GraphSetup.setup_graph`; the analyst sequence + parallelism plan is computed in `tradingagents/graph/analyst_execution.py:build_analyst_execution_plan`.

`AgentState` (in `tradingagents/agents/utils/agent_states.py`) is a `MessagesState` subclass that carries `company_of_interest`, `trade_date`, `asset_type`, the four analyst reports, the bull/bear debate state, the trader plan, the risk debate state, the final decision, and a `past_context` string injected from the memory log at run start.

### Wire keys vs user-facing labels

`"social"` is the wire key for the Sentiment Analyst everywhere (state keys, tool node names, `AnalystType.SOCIAL = "social"`). The label "Sentiment Analyst" is what the CLI and reports show. **Do not** rename the wire key — saved configs depend on it. The same back-compat shim re-exports `create_social_media_analyst` from `tradingagents/agents/__init__.py`.

### LLM client layer (`tradingagents/llm_clients/`)

- `factory.py:create_llm_client(provider, model, base_url=None, **kwargs)` is the single entry point. OpenAI-compatible providers (openai, xai, deepseek, qwen, qwen-cn, glm, glm-cn, minimax, minimax-cn, ollama, openrouter) all go through `openai_client.py:NormalizedChatOpenAI`. Anthropic, Google, and Azure have dedicated clients. Imports are lazy so the suite can collect without every SDK installed.
- **`capabilities.py` is the single source of truth for per-model quirks** — `tool_choice` support, structured-output method, `reasoning_split`, `reasoning_content` roundtripping. Adding a new model with a quirk means a row in `_BY_ID` (exact match) or `_BY_PATTERN` (forward-compat regex like `^deepseek-v\d`). Do not add `if model_name == ...` ladders in client code.
- `api_key_env.py:PROVIDER_API_KEY_ENV` maps each provider to its env var. Adding a provider requires registering it here so the CLI's interactive key-prompt flow finds it.

### Dataflow layer (`tradingagents/dataflows/`)

`interface.py` defines vendor-routed data tools (`get_stock_data`, `get_news`, `get_fundamentals`, …) with category-level (`data_vendors`) and tool-level (`tool_vendors`) configuration. `route_to_vendor` falls back to the next vendor **only** on `AlphaVantageRateLimitError`; any other exception propagates. Vendors today: `yfinance` (default) and `alpha_vantage`.

The Sentiment Analyst is an exception — it **pre-fetches** news, StockTwits, and Reddit data directly (no tool-calling) and injects them as prompt blocks before the first LLM turn. This was a deliberate fix for #557 (the old social analyst would hallucinate Reddit posts when only Yahoo News was available).

### Configuration

`tradingagents/default_config.py:DEFAULT_CONFIG` is the canonical dict. **Every key with a `TRADINGAGENTS_*` env-var maps to it via `_ENV_OVERRIDES`** with type-aware coercion (bool/int/string). To expose a new key over env, add a row in `_ENV_OVERRIDES` — no entrypoint changes needed.

Runtime overrides flow through `tradingagents/dataflows/config.py:set_config`, which **merges dict-valued keys one level deep** (e.g. `data_vendors`) and replaces scalars — partial updates don't clobber sibling defaults.

### Persistence

Two independent mechanisms, easy to confuse:

| | Decision log | Checkpoints |
|---|---|---|
| Always on? | Yes | Opt-in via `--checkpoint` |
| Location | `~/.tradingagents/memory/trading_memory.md` | `~/.tradingagents/cache/checkpoints/<TICKER>.db` |
| Override env | `TRADINGAGENTS_MEMORY_LOG_PATH` | `TRADINGAGENTS_CACHE_DIR` (base) |
| Format | Append-only markdown with `<!-- ENTRY_END -->` delimiters | LangGraph `SqliteSaver` per ticker |
| Purpose | Carry past decisions + realised returns into next run's PM prompt | Resume a crashed run from the last successful node |
| Lifecycle | Entries written as `pending`, resolved on next same-ticker run by `_resolve_pending_entries` → fetches return + alpha vs benchmark → writes reflection | Cleared on successful completion; `clear_all_checkpoints` for manual wipe |

`thread_id(ticker, date)` is a 16-char SHA-256 prefix so same ticker+date resumes, different date starts fresh.

### Structured output

Only the three **decision-making** agents (Research Manager, Trader, Portfolio Manager) use Pydantic schemas (`tradingagents/agents/schemas.py`); the analysts and debaters produce free-form prose. Each provider gets its native structured-output mode (`json_schema` for OpenAI/xAI, `response_schema` for Gemini, tool-use for Anthropic, function-calling for OpenAI-compatible). Render helpers turn parsed instances back into the same markdown shape downstream code already parses, so memory log, CLI, and saved reports keep working.

### Benchmark resolution for alpha

`TradingAgentsGraph._resolve_benchmark` matches the ticker's exchange suffix against `config["benchmark_map"]` (`.NS`→`^NSEI`, `.T`→`^N225`, `.HK`→`^HSI`, …) so non-US tickers don't get scored against SPY in USD. `config["benchmark_ticker"]` (when set) overrides everything. The reflection's label string includes the benchmark name dynamically.

## Adding a new LLM provider (checklist)

1. If it's OpenAI-compatible, add the lowercase name to `_OPENAI_COMPATIBLE` in `llm_clients/factory.py`. Otherwise create a new `{name}_client.py` and add a branch.
2. Register its env var in `llm_clients/api_key_env.py:PROVIDER_API_KEY_ENV`.
3. If models have non-default quirks, add rows in `llm_clients/capabilities.py:_BY_ID` (and/or `_BY_PATTERN` for forward-compat).
4. Surface in the CLI provider dropdown via `cli/main.py` (and the model lists in `cli/utils.py` / `llm_clients/model_catalog.py`).
5. Add the env var to `.env.example` and to `tests/conftest.py:_API_KEY_ENV_VARS`.

## Security note

Tickers come from user CLI input *and* from LLM tool calls (which can be influenced by prompt injection in fetched news). Every site that interpolates a ticker into a filesystem path **must** go through `tradingagents/dataflows/utils.py:safe_ticker_component`. The function allows `[A-Za-z0-9._\-\^]+` (caret needed for `^GSPC`-style index symbols), rejects all-dots and over-long inputs. Current call sites: results dir, checkpoint DB path, and cache.

## Windows-specific gotcha

`run_crm.py` reconfigures `sys.stdout`/`sys.stderr` to UTF-8 before any imports — analyst debug output contains Greek letters (σ for volatility, etc.) that cp1252 cannot encode. If you write a new debug-streaming entry-point script, copy that preamble.

## Tooling: the `superpowers` plugin is installed — use it

This environment has the **superpowers** Claude Code plugin installed. Its skills define *how* to do work in this repo, not just *what* to do. Invoke them via the `Skill` tool **before** writing code, not after the fact. The system-reminder skill index will list them at session start; the ones most relevant to this codebase:

**TDD is mandatory for every code change in this repo.** Before writing implementation code for any feature or bugfix, invoke `superpowers:test-driven-development` and write the failing test first. No exceptions for "small" changes — the provider/capability surface is tightly guarded by regression tests (capability table, MiniMax/DeepSeek/Anthropic quirks, dual-region routing) and skipping the red-test step is the fastest way to silently break one.

| Situation | Skill | Why it matters here |
|---|---|---|
| About to build/modify/extend any feature | `superpowers:brainstorming` | Required before creative work. Explores intent + edges before code lands. |
| Have a spec, about to touch multi-file work | `superpowers:writing-plans` | Produces the plan that downstream sessions execute. |
| Implementing a feature or bugfix | `superpowers:test-driven-development` | **Mandatory.** Rigid skill — write the failing test first. See bolded rule above. |
| Any bug, test failure, or unexpected behavior | `superpowers:systematic-debugging` | Rigid — root-cause before fixing. The capability table and dual-region provider routing have non-obvious failure modes. |
| About to claim work is done / commit / PR | `superpowers:verification-before-completion` | Run verification commands and confirm output before saying "passing." Evidence before assertions. |
| Executing an existing implementation plan | `superpowers:executing-plans` or `superpowers:subagent-driven-development` | Pick the latter when plan tasks are independent. |
| 2+ independent investigations | `superpowers:dispatching-parallel-agents` | Use for exploring multiple providers, multiple data vendors, or multiple test failures in parallel. |
| Feature work needing isolation | `superpowers:using-git-worktrees` | Before starting parallel branches. |
| Done implementing | `superpowers:finishing-a-development-branch` | Guides merge/PR/cleanup decision. |
| Receiving review feedback | `superpowers:receiving-code-review` | Technical rigor over performative agreement. |

The `superpowers:using-superpowers` meta-skill spells out the rule: **if there's even a 1% chance a skill applies, invoke it first.** Skills override default system behavior; user instructions in this file override skills.

Other plugins that are installed and worth using when relevant:
- `codex:rescue` — delegate to Codex for a second-opinion investigation or substantial coding handoff
- `code-review:code-review`, `feature-dev:*`, `frontend-design:frontend-design` — domain-specific workflows
- `claude-api` — when touching any code that imports `anthropic` or `@anthropic-ai/sdk` (note: this repo uses `langchain-anthropic`, not the raw SDK, so this triggers less often)
- `verify`, `simplify`, `run`, `commit-commands:*` — operational helpers

## MCP servers wired up here

- **`coolify`** — manages this project's Coolify deployment (apps, databases, servers, env vars, deploys, logs). Reach for `mcp__coolify__*` tools when asked about deployment status, server health, env var changes, redeploys, or production logs. Don't shell out to a `coolify` CLI; use the MCP.

## Files that look unimportant but are load-bearing

- `tradingagents/graph/analyst_execution.py` — the `AnalystNodeSpec` table is what keeps wire-key/label/report-key alignment correct. Touch this if you add a new analyst type.
- `tradingagents/agents/schemas.py` — the field `description=` strings are the model's output instructions; rewording them changes model behavior.
- `tests/conftest.py` — drop a new provider's env var into `_API_KEY_ENV_VARS` or its tests will hang in CI.
- `pyproject.toml` `[project.scripts]` — `tradingagents = "cli.main:app"` is what makes the console script work after `pip install .` (issue #747).
