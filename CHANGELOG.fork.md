# Changelog (fork)

All notable changes to **[`kskylespence/TradingAgents`](https://github.com/kskylespence/TradingAgents)**
beyond the upstream baseline are documented here. For changes that came
in from upstream `TauricResearch/TradingAgents`, see the sibling
[`CHANGELOG.md`](./CHANGELOG.md) — that file is the unchanged upstream
mirror and is not edited by this fork.

Format: [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).
Versioning: PEP 440 local segment `<upstream-version>+hf.<N>` (e.g.
`0.2.5+hf.1`). The `+hf.N` counter resets to `1` whenever the upstream
base version moves forward. See [`docs/RELEASING.md`](./docs/RELEASING.md)
for the per-deploy cut workflow.

## [Unreleased]

### Added

### Changed

- **Dropped the bespoke `_is_https()` / `is_secure_request()` helpers.**
  Replaced with `request.url.scheme == "https"`, which Starlette's
  `ProxyHeadersMiddleware` (enabled via uvicorn `--proxy-headers`) sets
  correctly behind a trusted reverse proxy. The previous helpers parsed
  the raw `X-Forwarded-Proto` header directly, which would have been
  attacker-controllable in any deployment topology without a strict
  proxy whitelist.

### Fixed

### Removed

### Security

- **Three production secrets now hard-required at startup.** `JWT_SECRET`,
  `FERNET_KEY`, and `ADMIN_PASSWORD_HASH` no longer have defaults in
  `web/backend/app/config.py`. Pydantic refuses to construct `Settings`
  if any is unset or shorter than its enforced `min_length` (32 / 44 /
  60). Closes the misconfigured-deploy failure mode where the previous
  `default="dev-jwt-secret-change-me"` left a publicly-known signing key
  in effect whenever an operator forgot to set the env var — anyone
  reading the public fork could have forged a valid admin JWT.
- **Login rate-limit no longer bypassable via spoofed `X-Forwarded-For`.**
  `services/rate_limit.py:client_ip` previously read the leftmost XFF
  entry, which is directly attacker-controllable even behind a trusted
  proxy. Now reads `request.client.host`, populated from the trusted
  proxy hop by uvicorn's `--proxy-headers --forwarded-allow-ips='*'`
  flag added to `entrypoint.sh`. Without this fix, an attacker could
  rotate spoofed XFF on every `/api/auth/login` call and brute-force the
  admin password at unrestricted rate.
- **Content-Security-Policy `script-src` no longer permits
  `'unsafe-inline'`.** The Vite production build emits only external
  module scripts, so the directive was unused but dangerous: a future
  commit adding `rehype-raw` to the report renderer would have turned
  any LLM-injected `<img onerror=>` into stored XSS without CSP
  catching it. `style-src` keeps `'unsafe-inline'` because Tailwind
  needs it for utility-class injection.
- **Container runs as non-root `tradingagents` (UID 10001).** Dockerfile
  adds the user, chowns `/data/tradingagents` and `/app`, and switches
  via `USER tradingagents` before the entrypoint. Limits the blast
  radius of any code-execution attack against the running process —
  previously a successful exploit landed as root with full write access
  to the mounted volume.

## [0.2.5+hf.1] — 2026-05-21

First cut of the fork as a versioned artifact. Captures every fork-only
commit landed on `main` between upstream tag `v0.2.5` and this release —
notably the Web UI surface, the Coolify-targeted deploy pipeline, and a
handful of upstream-PR backports that hadn't yet shipped in upstream
`main`.

**Baseline:** built on [`TauricResearch/TradingAgents` v0.2.5](https://github.com/TauricResearch/TradingAgents/releases/tag/v0.2.5).

### Added

- **Web UI — single-tenant FastAPI backend + React frontend.** A
  login-gated browser interface to the trading-agents pipeline: kick off
  runs, watch agent status and reports stream in over SSE, browse run
  history, manage per-provider API keys. Backend uses an auto-discovery
  registry pattern for routers / middleware / lifespan hooks so adding a
  new endpoint is "drop a file, no wiring." Frontend is Vite + React 18
  + TypeScript 5 + Tailwind 3 + shadcn primitives, with a typed
  `RunEvent` discriminated union shared between the SSE stream and the
  client reducer. Ships with an e2e Playwright suite and a pytest backend
  suite that runs against an in-memory SQLite engine.
- **Multi-stage Docker build + Coolify deploy guide.** One image bundles
  the backend, the built frontend assets, and the `tradingagents` engine
  itself; `entrypoint.sh` runs Alembic migrations on boot and then
  `uvicorn --proxy-headers`. A `DEPLOY.md` walks through the Coolify
  side: required env vars (`JWT_SECRET`, `FERNET_KEY`,
  `ADMIN_PASSWORD_HASH`), volume mounts for `~/.tradingagents`, and the
  health-check contract.
- **`OLLAMA_API_KEY` env var for Ollama Cloud auth.** The OpenAI-
  compatible Ollama client now picks up an API key when present, so the
  same code path works against a self-hosted `ollama serve` (no auth) and
  the managed Ollama Cloud endpoint (bearer token required).
- **Analysis-only crypto asset mode.** A new `tradingagents/asset_types.py`
  module centralises crypto-vs-equity detection (suffix-based:
  `-USD`, `-USDT`, etc.) and the analyst-set filtering rules — crypto
  runs skip fundamentals and route market/news/sentiment through the
  crypto-appropriate data path. Integrates the design from upstream
  PR #567 plus the analyst-execution-plan scaffolding from PR #487.
- **`tradingagents/run_observer.py` — shared streaming module.** Extracts
  `RunObserver` + `stream_run` out of `cli/main.py` so both the CLI and
  the web backend can drive a graph run identically, emitting the same
  event taxonomy. The CLI now consumes this module rather than owning
  its own observer.
- **`tradingagents/providers.py` and shared catalog/env-var modules.**
  `PROVIDERS`, `MODEL_OPTIONS`, and `PROVIDER_API_KEY_ENV` are now
  importable from a single place; the CLI dropdown and the web settings
  page consume the same source of truth, eliminating the previous
  duplicate provider lists.
- **`run_crm.py` programmatic one-off driver.** A small Windows-safe
  entry point (reconfigures stdout/stderr to UTF-8 before the trading-
  agents imports load) for batch / cron-style invocations that don't
  want the interactive CLI.
- **Root `CLAUDE.md` + nested `web/backend/CLAUDE.md` + `web/frontend/CLAUDE.md`.**
  Authoritative guidance for AI coding sessions: registry patterns,
  security invariants, async-loop conventions, and the load-bearing-files
  list per subtree.
- **Comprehensive Web UI developer docs** under `web/docs/` — architecture,
  API reference + SSE event taxonomy, backend-dev guide,
  frontend-dev guide, testing conventions, and operations runbook.

### Changed

- **Shared modules extracted from `cli/`.** Provider and asset-type logic
  moved out of `cli/utils.py` so the web backend can reuse them without
  pulling in CLI-only dependencies. CLI behaviour is unchanged; the
  imports just resolve to the shared modules now.
- **`cli/main.py` slimmed down.** Run streaming + observer logic moved to
  `tradingagents/run_observer.py`; `cli/main.py` is now a thin Typer
  layer that wires the observer to Rich console output. Same UX, fewer
  responsibilities per file.

### Fixed

- **Anthropic `effort=` kwarg skipped on non-supporting models** (backport
  of upstream PR #831). Older Claude models reject the `effort` parameter
  the model catalog now sends to newer ones; the binding flow consults
  the capability table and omits the kwarg where the model doesn't
  declare support.
- **MiniMax `reasoning_split` gated by model capability** (backport of
  upstream PR #826). Only M2.x models that emit the
  `reasoning_content` channel get the split treatment; older MiniMax
  models that pack reasoning into the main content stream are left
  alone.
- **Sentiment analyst label + asset-type propagation through the graph.**
  Integrates upstream PRs #487 + #567 cleanly: sentiment label stays
  "Sentiment Analyst" across the CLI / status panel / saved reports,
  the asset-type flag is threaded through `propagation.py` so analyst
  filtering activates, and the analyst execution plan honours the
  filtered set.
- **Web backend routers auto-discover before `tradingagents` is
  installed.** The Coolify build originally failed when the backend
  package was imported before `pip install -e .` of the engine; the
  Dockerfile ordering is fixed and the auto-discovery loader tolerates
  partial imports during boot.

[Unreleased]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5-hf.1...HEAD
[0.2.5+hf.1]: https://github.com/kskylespence/TradingAgents/compare/v0.2.5...v0.2.5-hf.1
