# Deploying TradingAgents to Coolify

This guide walks you through deploying the TradingAgents web UI to a
[Coolify](https://coolify.io/)-managed VPS. The target topology is a single
Docker image (built from the repo-root `Dockerfile`) talking to **Postgres**
(either a **Coolify-managed database on the same server** — recommended for a
single VPS — or a hosted [Neon](https://neon.tech/) instance) plus a
Coolify-managed persistent volume mounted at `/data/tradingagents`.

The build pack is **Dockerfile**, the exposed port is **8000**, and the
health-check endpoint is **`/api/health`**. The image runs `alembic upgrade
head` on every container start before booting uvicorn, so migrations are
applied automatically on deploy.

> **Apache 2.0.** This deployment guide is part of the TradingAgents
> project and is distributed under the same license as the rest of the
> repository. See `LICENSE`.

---

## Prerequisites

Before you start, you should have:

- A VPS running Coolify (any recent stable release). The Coolify "Server" page
  should report healthy.
- DNS for your chosen hostname pointed at the VPS public IP (an A record for
  `tradingagents.example.com` is enough).
- **A Postgres database** for the app — either:
  - **Coolify Postgres (recommended on a single VPS):** create a standalone
    PostgreSQL resource in the same Coolify project/environment (see
    [Step 3b](#step-3b--database-choose-one)); or
  - **Neon:** a project with a database called `tradingagents`, pooled
    connection string converted to `postgresql+asyncpg://…?ssl=require`.
- A workstation with Python 3.10+ installed locally, used only to generate
  secrets (the secrets never leave your machine until you paste them into
  Coolify).
- LLM provider credentials for whichever providers you plan to use. For the
  **lite VPS preset** (Ollama Cloud, no local inference), set
  `OLLAMA_BASE_URL=https://ollama.com/v1` and `OLLAMA_API_KEY`. Other
  providers can be added later via the in-app **Settings** page.

You do **not** need Docker on your workstation; Coolify builds the image on
the VPS itself. For small VPS hosts (2 vCPU / 8 GB), prefer a
[prebuilt image from GHCR](#prebuilt-image-ghcr) so deploy does not
saturate CPU during `npm ci` + `pip install`.

---

## VPS sizing

| Topology | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| Web UI + **cloud LLM** (OpenAI, Anthropic, Groq, Ollama Cloud) | 2 vCPU, 4 GB RAM | 2 vCPU, 8 GB RAM | The app is mostly idle between runs; analysis cost is API-side. |
| **Docker build on VPS** (default Coolify path) | 2 vCPU, 8 GB RAM | 4 vCPU, 8 GB+ RAM | Build pegs CPU for 3–5 minutes (`npm ci`, Vite, dual `pip install`). Add [swap](#swap-for-deploy-builds) or use GHCR. |
| Web UI + **local Ollama** on the same host | Not recommended | 4 vCPU, 16 GB RAM | Inference pegs CPU; 7B+ models need RAM the web stack also uses. Run Ollama on a **separate** machine and set `OLLAMA_BASE_URL` to it. |

The web server itself is a single uvicorn process with no worker pool — it
does not need horizontal scaling for v1 (one analysis run at a time via
`GLOBAL_RUN_LOCK`).

### Swap for deploy builds

On a 2 vCPU / 8 GB VPS, add swap before the first Coolify build so a
memory spike during `npm run build` does not trigger the OOM killer:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Swap does not fix local Ollama inference — it only cushions one-time
deploy builds.

### Prebuilt image (GHCR)

Coolify can pull a prebuilt image instead of building on your VPS. The
repo publishes to GitHub Container Registry on every `v*-hf.*` tag (see
[`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)):

1. Cut a release tag per [`docs/RELEASING.md`](docs/RELEASING.md) (e.g.
   `v0.2.5-hf.7`).
2. Wait for the **Publish Docker image** workflow to finish on GitHub
   Actions.
3. In Coolify, switch the application from **Dockerfile** build to
   **Docker image** and set:

   ```
   ghcr.io/kskylespence/tradingagents:0.2.5-hf.7
   ```

   (Use the PEP 440 version with `+` replaced by `-` in the tag, or
   `:latest` for the most recent tagged build.)

4. Make the package public once under GitHub → Packages → TradingAgents →
   Package settings, or configure Coolify with a read-only `GITHUB_TOKEN`
   / PAT that has `read:packages`.

This removes the deploy-time CPU spike entirely — only container start +
`alembic upgrade head` run on the VPS.

---

## Step 1 — Generate secrets

Run these locally. Store the output somewhere safe (a password manager) — you
will paste each value into Coolify in Step 4. **Never commit these to git.**

### Admin password hash (bcrypt)

```bash
python -c "from passlib.hash import bcrypt; print(bcrypt.using(rounds=12).hash('your-password'))"
```

Replace `your-password` with the password you want to use to log in to the UI.
The output starts with `$2b$12$...` — that whole string is the value of
`ADMIN_PASSWORD_HASH`.

> **Coolify-specific gotcha — the `$`-mangling trap.** Coolify silently
> interpolates `$<name>` references inside env-var values *regardless* of
> the `is_literal` / `is_multiline` flags exposed by its API and UI.
> bcrypt hashes always contain three `$` characters (`$2b$<cost>$<salt
> +digest>`), so the third segment gets dropped to the empty string and
> your container receives a 45-46-char "hash" that bcrypt cannot verify
> against any password.
>
> If you observe `Got length 46` in the deploy logs' pydantic
> ValidationError, you've hit this trap. The fix is to set
> `ADMIN_PASSWORD_HASH_B64` instead — the base64 of the hash, which has
> no `$` characters and round-trips cleanly:
>
> ```bash
> python -c "from passlib.hash import bcrypt, sys; import base64; \
>   h = bcrypt.using(rounds=12).hash('your-password'); \
>   print('ADMIN_PASSWORD_HASH_B64=' + base64.b64encode(h.encode()).decode())"
> ```
>
> The backend's config validator decodes the b64 form into
> `admin_password_hash` at boot. Set **either** `ADMIN_PASSWORD_HASH`
> **or** `ADMIN_PASSWORD_HASH_B64` (not both — the direct field wins if
> populated). On Coolify specifically, prefer the `_B64` form. The same
> trap applies to any future env value containing `$` followed by
> alphabetic chars; `FERNET_KEY` and `JWT_SECRET` aren't affected
> because they're hex / url-safe-base64 with no leading-letter dollar
> references.

### JWT signing key

```bash
openssl rand -hex 32
```

64 hex characters. This becomes `JWT_SECRET`. Rotating it invalidates every
existing session, so it is also your "log out everywhere" lever.

### Fernet encryption key (for stored provider keys)

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

44 url-safe base64 characters ending in `=`. This becomes `FERNET_KEY`.
**Losing this key permanently locks any provider API keys you stored in the
database** — back it up.

---

## Step 2 — Create the Coolify application

1. In Coolify, click **+ New** → **Application**.
2. Pick the source: connect your GitHub/Gitea/Forgejo account (or use the
   "Public Repository" option) and point Coolify at this repository on the
   `main` branch.
3. **Build pack**: choose **Dockerfile**. Coolify will auto-detect the
   `Dockerfile` at the repo root.
4. **Port**: set the exposed port to **8000**.
5. **Build command**: leave empty (the Dockerfile drives everything).
6. **Start command**: leave empty (the Dockerfile has an `ENTRYPOINT`).
7. Save. **Do not deploy yet** — you still need to attach a volume and set
   env vars.

---

## Step 3 — Attach a persistent volume

Reports, the LangGraph crash-resume checkpoint SQLite, and the memory log
all live on disk. Coolify provides "Persistent Storage" per-application.

1. Open your new app → **Storage** tab.
2. Click **+ Add**.
3. **Source path** (host): leave Coolify to auto-name it (Coolify appends a
   UUID).
4. **Destination path** (container): `/data/tradingagents`
5. **Type**: Volume.
6. Save.

The container's entrypoint creates `logs/`, `cache/`, `memory/`, and
`reports/` subdirectories under that path on first boot.

> **Volume permissions gotcha.** The container runs as the non-root
> `tradingagents` user (UID 10001). The Dockerfile pre-creates
> `/data/tradingagents` and `chown`s it during the image build, so a
> fresh Coolify-provisioned volume mounted at that path inherits the
> correct ownership on first boot. If you mount a pre-existing host
> directory with restrictive ownership, you must `chown` it to UID
> 10001 — see Troubleshooting.

---

## Step 3b — Database (choose one)

The web UI stores runs, SSE replay events, encrypted API keys, saved form
defaults, and login rate-limit state in Postgres. Pick **one** backend.

### Option A — Coolify Postgres (recommended for a single VPS)

1. In Coolify → your project → **+ New** → **Database** → **PostgreSQL**.
2. Name it `tradingagents` (user/db can match). Generate a strong password.
3. Deploy/start the database until its status is **running:healthy**.
4. Copy the **internal** connection string Coolify shows (host is the
   database resource UUID on the `coolify` Docker network, not a public IP).
5. Convert the scheme for SQLAlchemy async:

   ```text
   postgres://USER:PASS@<db-uuid>:5432/tradingagents
   → postgresql+asyncpg://USER:PASS@<db-uuid>:5432/tradingagents
   ```

   No `?ssl=require` is needed for container-to-container traffic on the
   same Coolify network.

6. Paste the result into `DATABASE_URL` on the **application** (Step 4).

The app and database must live in the **same Coolify environment** so they
share the `coolify` network.

### Option B — Neon (managed Postgres)

1. Create a Neon project and database named `tradingagents`.
2. Copy the **pooled** connection string from the Neon dashboard.
3. Convert to async SQLAlchemy and **append TLS**:

   ```text
   postgresql+asyncpg://USER:PASS@ep-xxxx.neon.tech/tradingagents?ssl=require
   ```

Neon is a good fit when the database should outlive the VPS or you want
managed point-in-time recovery without operating Postgres yourself.

---

## Step 4 — Environment variables

Open the **Environment Variables** tab on your Coolify app. Add every entry
below. For **every** secret (anything but `DATA_DIR`, `RETENTION_DAYS`,
`JWT_TTL_SECONDS`, and the Coolify magic vars), tick the **"Runtime only"**
checkbox so the value is **not baked into image layers** — Coolify will inject
it only into the running container.

| Name                  | Required | Runtime-only | Example / source                                                                 |
|-----------------------|----------|--------------|----------------------------------------------------------------------------------|
| `ADMIN_USERNAME`      | yes      | yes          | `admin`                                                                          |
| `ADMIN_PASSWORD_HASH` | yes¹     | yes          | output of the bcrypt command (Step 1) — **see `$`-mangling note**                |
| `ADMIN_PASSWORD_HASH_B64` | yes¹ | yes          | base64 of the bcrypt hash; **required on Coolify** (the `$`-mangling fix)       |
| `JWT_SECRET`          | yes      | yes          | output of `openssl rand -hex 32` (Step 1)                                        |
| `JWT_TTL_SECONDS`     | no       | no           | `604800` (7 days; default if unset)                                              |
| `FERNET_KEY`          | yes      | yes          | output of the Fernet command (Step 1)                                            |
| `DATABASE_URL`        | yes      | yes          | See [Step 3b](#step-3b--database-choose-one). Coolify Postgres: `postgresql+asyncpg://user:pass@<db-uuid>:5432/tradingagents`. Neon: `…neon.tech/…?ssl=require` |
| `DATA_DIR`            | no       | no           | `/data/tradingagents` (default; matches the volume mount)                        |
| `RETENTION_DAYS`      | no       | no           | `90`                                                                             |
| `COOLIFY_FQDN`        | auto     | n/a          | auto-injected by Coolify                                                         |
| `COOLIFY_URL`         | auto     | n/a          | auto-injected by Coolify                                                         |
| `OPENAI_API_KEY`      | optional | yes          | pre-seed a provider key to skip the in-app Settings step                         |
| `ANTHROPIC_API_KEY`   | optional | yes          | same                                                                             |
| `GOOGLE_API_KEY`      | optional | yes          | same                                                                             |
| `DASHSCOPE_API_KEY`   | optional | yes          | Alibaba Qwen (international)                                                     |
| `DASHSCOPE_CN_API_KEY`| optional | yes          | Alibaba Qwen (mainland China)                                                    |
| `MOONSHOT_API_KEY`    | optional | yes          | Moonshot / Kimi                                                                  |
| `DEEPSEEK_API_KEY`    | optional | yes          | DeepSeek                                                                         |
| `ZHIPUAI_API_KEY`     | optional | yes          | Zhipu GLM (international)                                                        |
| `ZHIPUAI_CN_API_KEY`  | optional | yes          | Zhipu GLM (mainland China)                                                       |
| `MINIMAX_API_KEY`     | optional | yes          | MiniMax                                                                          |
| `OPENROUTER_API_KEY`  | optional | yes          | OpenRouter                                                                       |
| `TOGETHER_API_KEY`    | optional | yes          | Together AI                                                                      |
| `XAI_API_KEY`         | optional | yes          | xAI Grok                                                                         |
| `OLLAMA_BASE_URL`     | optional | yes          | `https://ollama.com/v1` for Ollama Cloud; omit if not using Ollama              |
| `OLLAMA_API_KEY`      | optional | yes          | Ollama Cloud API key (required when `OLLAMA_BASE_URL` points at ollama.com)     |
| `TRADINGAGENTS_LLM_PROVIDER` | optional | yes   | `ollama` for the [lite VPS preset](web/docs/operations.md#lite-vps-preset)      |
| `TRADINGAGENTS_QUICK_THINK_LLM` | optional | yes | `glm-5.2` (lite preset default)                                                |
| `TRADINGAGENTS_DEEP_THINK_LLM`  | optional | yes | `glm-5.2` (lite preset default)                                                |
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` | optional | yes | `1` (lite preset)                                                             |
| `TRADINGAGENTS_MAX_RISK_ROUNDS`   | optional | yes | `1` (lite preset)                                                             |
| `TRADINGAGENTS_RUN_MAX_SECONDS`   | optional | yes | `1200` (lite preset)                                                          |
| `TRADINGAGENTS_LLM_READ_TIMEOUT` | optional | yes | `90` (lite preset)                                                            |
| `TRADINGAGENTS_LLM_MAX_RETRIES`  | optional | yes | `3` (lite preset)                                                             |

¹ Exactly one of `ADMIN_PASSWORD_HASH` / `ADMIN_PASSWORD_HASH_B64` is
required. The b64 form is the workaround for Coolify's `$`-interpolation
bug documented in Step 1. The direct field wins if both are set.

The eleven provider key variables are optional at deploy time — any not set
here can be added later through the **Settings → API Keys** page in the UI,
which encrypts them with `FERNET_KEY` and stores them in Postgres.

> **Neon SSL.** If you use Neon, your `DATABASE_URL` **must** end in
> `?ssl=require`. asyncpg will not negotiate TLS automatically and Neon
> refuses unencrypted connections. Coolify Postgres on the internal
> Docker network does not need this suffix. If you forget on Neon, the
> entrypoint's `alembic upgrade head` will fail with
> `SSL connection has been closed unexpectedly` — see Troubleshooting.

---

## Step 5 — Custom domain + TLS

1. In Coolify → app → **Domains** tab, add your hostname with the scheme
   included — e.g. `https://tradingagents.example.com` (not bare
   `tradingagents.example.com`). Coolify generates Traefik labels from
   this value; omitting `https://` produces a broken `Host(``)` rule and
   Traefik will respond with **"no available server"**.
2. Save. Coolify provisions a Let's Encrypt certificate automatically through
   its bundled Traefik proxy; this typically completes within ~30 seconds of
   the next deploy.
3. If you have multiple domains pointing at the same app, list them
   comma-separated, each with its scheme (`https://…`).

No additional reverse-proxy configuration is required — Coolify's Traefik
handles HTTP→HTTPS redirect and HSTS termination on its own.

---

## Step 6 — Health checks

Two independent health checks are configured by the Dockerfile and Coolify:

1. **Docker `HEALTHCHECK`** (baked into the image):

   ```dockerfile
   HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
     CMD curl -fsS http://localhost:8000/api/health || exit 1
   ```

   Coolify honors this and surfaces the status in the UI.

2. **Coolify UI health check** (set explicitly so Coolify can use it during
   zero-downtime swaps):

   - Path: `/api/health`
   - Port: `8000`
   - Scheme: `http`
   - Interval: `30s`
   - Timeout: `5s`
   - Retries: `3`

The endpoint returns `{"status": "ok", "version": "…", "db": "ok", …}`
when everything is healthy. `db: "down"` indicates the Postgres connection is
broken; the container still serves UI assets so you can see the error. When
`TRADINGAGENTS_LLM_PROVIDER=ollama`, the body also includes an `ollama`
upstream probe block (`status`, `model_count`, `circuit_state`).

---

## Step 7 — Deploy and smoke test

1. Click **Deploy** in Coolify. Watch the build logs:

   ```text
   Step ../.. : FROM node:20-alpine AS fe
   Step ../.. : FROM python:3.12-slim AS be
   ...
   Successfully tagged coolify/...:latest
   ```

   The first build takes ~3-5 minutes (npm install + pip install).

2. Wait for the Coolify app card to flip to **Running** and the health check
   to turn green.

3. From your workstation, smoke-check the health endpoint:

   ```bash
   curl -fsS https://tradingagents.example.com/api/health | jq .
   ```

   Expected:

   ```json
   {
     "status": "ok",
     "db": "ok",
     "disk_free_mb": 18234,
     "active_run_id": null
   }
   ```

4. Open `https://tradingagents.example.com` in your browser, log in with
   `ADMIN_USERNAME` + the plaintext password you hashed in Step 1.

5. Submit a small smoke run (lite preset example):

   - Ticker: `SPY`
   - Date: today
   - Analysts: `market`, `social` (Sentiment)
   - Depth: `1` (Shallow)
   - Provider: `ollama` (Ollama Cloud — requires `OLLAMA_BASE_URL` +
     `OLLAMA_API_KEY` in Step 4)
   - Quick model: `glm-5.2`
   - Deep model: `glm-5.2`

   The run takes a few minutes at depth=1 with a cloud model. Watch the agent
   grid turn green and the final 5-tier rating (`Buy / Overweight / Hold /
   Underweight / Sell`) appear. Click **Download Report** to confirm the
   markdown bundle.

   > **OpenAI smoke alternative:** swap provider to `openai`, set
   > `OPENAI_API_KEY`, and pick `gpt-4o-mini` for both models if you are not
   > using Ollama Cloud.

6. Verify the database row exists (Coolify Postgres terminal, `psql`, or Neon
   SQL editor):

   ```sql
   SELECT id, ticker, status, rating, started_at, finished_at
   FROM runs
   ORDER BY created_at DESC
   LIMIT 5;
   ```

7. Verify the volume report was written. SSH into the VPS (or use Coolify's
   **Terminal** tab on the app):

   ```bash
   ls -la /data/tradingagents/logs/SPY/
   ls -la /data/tradingagents/reports/
   ```

   You should see at least one timestamped subdirectory containing the
   per-analyst JSON + markdown files.

If all seven of these pass, the deploy is good.

---

## Troubleshooting

### Volume permissions: `PermissionError: [Errno 13] Permission denied: '/data/tradingagents/...'`

The container runs as the non-root `tradingagents` user (UID 10001).
The Dockerfile `chown`s `/data/tradingagents` at build time, so a
fresh Coolify volume mounted at that path inherits correct ownership
on first boot. If you mounted a pre-existing host directory with
restrictive ownership, fix it from the Coolify terminal:

```bash
chown -R 10001:10001 /data/tradingagents
chmod -R u+rwX /data/tradingagents
```

(UID 10001 = `tradingagents`; the user is also pre-created in the
image.)

### Traefik returns `no available server` on your custom domain

The app container is healthy but the browser shows a plain-text
**no available server** page. Coolify generated a broken Traefik host
rule — almost always because the domain was saved **without** the
`https://` prefix.

Fix: open the app → **Domains** → set FQDN to `https://your.host.name`
→ save → **Restart** or **Deploy**. The Traefik rule should read
`Host(\`your.host.name\`)`, not `PathPrefix(\`your.host.name\`)`.

### `alembic upgrade head` fails on first boot

**Neon:** the most common cause is missing `?ssl=require` on the URL.
Coolify logs (app → **Logs** tab) will show:

```text
asyncpg.exceptions.ConnectionFailureError: SSL connection has been closed unexpectedly
```

Fix:

```text
DATABASE_URL=postgresql+asyncpg://user:pass@ep-xxxx.neon.tech/tradingagents?ssl=require
```

**Coolify Postgres:** confirm the database resource is `running:healthy`,
the app and DB are in the same environment, and `DATABASE_URL` uses the
**internal** hostname (the DB resource UUID), not `localhost` or the VPS
public IP. Scheme must be `postgresql+asyncpg://`.

After updating the env var, click **Restart** (not Deploy) in Coolify. A
restart is enough; you do not need to rebuild the image.

Other alembic failures to check:

- `relation "alembic_version" already exists` after a failed migration —
  connect to Postgres (`psql` or Neon SQL editor) and run
  `DROP TABLE alembic_version;`, then restart.
- `permission denied for schema public` — confirm the database role in the
  connection string owns the database.

### SSE stream drops every ~30 seconds

Coolify's bundled Traefik defaults to a 30-second idle-connection timeout.
The backend already emits `: keepalive\n\n` heartbeats every 15 seconds, but
if you've customized Traefik's `forwardingTimeouts.responseHeaderTimeout`
shorter than 15s, the stream will still close.

Confirm with:

```bash
docker inspect coolify-proxy | grep -A2 forwardingTimeouts
```

Stock Coolify ships safe defaults; only an admin-installed plugin or manual
edit to `/data/coolify/proxy/dynamic/*.yaml` is likely to be the culprit.

### Coolify build logs to grep first

When something fails, these are the most informative greps:

```bash
# Run from the VPS where the Coolify proxy lives.
docker logs coolify-proxy 2>&1 | grep -i tradingagents

# Application container logs (substitute the actual container name from
# `docker ps`).
docker logs <coolify-app-container> 2>&1 | tail -200

# Just alembic output:
docker logs <coolify-app-container> 2>&1 | grep -E "alembic|INFO  \[alembic"

# Just the SSE keepalive cadence (should print one heartbeat per 15s while a
# run is in progress):
docker logs <coolify-app-container> 2>&1 | grep -i keepalive
```

### VPS shut down or became unresponsive (CPU / OOM)

If the provider power-cycled the VM during deploy or a first analysis run,
see the full [VPS troubleshooting runbook](web/docs/operations.md#vps-troubleshooting).
Quick checks on the host:

```bash
sudo dmesg -T | grep -iE 'killed process|out of memory' | tail -20
docker stats --no-stream
systemctl status ollama 2>/dev/null
```

Most common fixes: [prebuilt GHCR image](#prebuilt-image-ghcr), cloud LLM
instead of local Ollama, [lite run settings](web/docs/operations.md#lite-vps-preset).

### Coolify failed deploy with `no space left on device`

Coolify keeps every previous image layer locally. Prune from the VPS:

```bash
docker system prune -af --volumes
```

(Stop the app first if you want to be safe — the `--volumes` flag will not
touch named volumes that are attached to running containers, but it will
delete unattached volumes.)

### Login returns 429 / I'm locked out

The login route is rate-limited to 5 failures per 5 minutes per IP. The
counter is also persisted to `login_attempts` in Postgres so a container
restart will not reset it. Clear via `psql`, the Coolify database
terminal, or the Neon SQL editor:

```sql
DELETE FROM login_attempts WHERE ip = '<your-ip>'::inet;
```

### The container won't stay running

Inspect the exit code and last log lines:

```bash
docker ps -a --filter "name=<coolify-app-container>" --format '{{.Status}}'
docker logs <coolify-app-container> 2>&1 | tail -50
```

Typical causes:

- Missing or short `JWT_SECRET` — pydantic-settings refuses to construct
  with `ValidationError` if the env var is unset or shorter than 32
  chars. The same hard-require applies to `FERNET_KEY` (≥ 44 chars) and
  `ADMIN_PASSWORD_HASH` (≥ 60 chars — a bcrypt hash is always 60).
  The error message names the offending field; regenerate from Step 1
  and redeploy.
- **`Got length 46` (or anything < 60) on `admin_password_hash`** — you
  set `ADMIN_PASSWORD_HASH` directly on Coolify and Coolify ate the
  `$<chars>` segments. Switch to `ADMIN_PASSWORD_HASH_B64` (Step 1) and
  delete the broken `ADMIN_PASSWORD_HASH` env var entirely.
- Bad `FERNET_KEY` content — must be 44 url-safe base64 chars produced
  by `Fernet.generate_key()`. A 44-char string of the wrong format
  passes length validation but fails when the app first tries to
  encrypt/decrypt (`FernetNotConfiguredError`).
- Wrong `DATABASE_URL` scheme — must be `postgresql+asyncpg://`, not bare
  `postgresql://`.

### Login returns 401 on the correct password (no rate-limit message)

If you're certain you're typing the right plaintext password and `/api/health`
reports `db: ok`, the most likely cause is that the bcrypt hash in
`os.environ` is corrupted — either by the `$`-mangling trap (see Step 1)
or by a stale duplicate `ADMIN_PASSWORD_HASH` entry overriding the b64
fallback. Diagnose from the Coolify terminal or SSH:

```bash
CTR=$(docker ps --filter "name=yrft8wjf" --format "{{.Names}}" | head -1)  # adjust UUID prefix
docker exec "$CTR" python -c "
import os, base64
from passlib.hash import bcrypt
h  = os.environ.get('ADMIN_PASSWORD_HASH', '')
b  = os.environ.get('ADMIN_PASSWORD_HASH_B64', '')
print('len(HASH)    =', len(h))
print('len(HASH_B64)=', len(b))
if b and not h:
    h = base64.b64decode(b).decode()
print('verify:', bcrypt.verify('your-plaintext-here', h) if len(h) >= 60 else 'hash too short')"
```

A `len(HASH)` value of 45–46 means Coolify mangled it. Delete the
`ADMIN_PASSWORD_HASH` entry from the Coolify UI (leaving
`ADMIN_PASSWORD_HASH_B64` in place) and **deploy** (not Restart — a
Coolify Restart reuses the container's existing env; only a Deploy spawns
a fresh container with the updated env).
