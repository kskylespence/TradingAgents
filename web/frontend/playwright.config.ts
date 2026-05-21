import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Playwright config for the TradingAgents web frontend.
 *
 * `webServer` automatically boots:
 *   1. The FastAPI backend on :8000 with FAKE_LLM=1 (no real LLM calls).
 *      `alembic upgrade head` runs first to materialise the SQLite schema.
 *   2. The Vite dev server on :5173, which proxies /api/* → :8000.
 *
 * Both processes are killed when Playwright exits. The backend uses an
 * on-disk SQLite file in a per-run tmp dir so concurrent runs don't
 * collide on `:memory:` lifetimes across worker processes.
 *
 * Required env (defaulted below — override only if you want to point at
 * an already-running stack):
 *   - PW_BASE_URL          (default http://localhost:5173)
 *   - PW_SKIP_WEBSERVER    set to "1" to disable auto-boot (then bring
 *                          your own backend+frontend before running)
 *
 * The bcrypt hash below corresponds to the password "password" for the
 * admin user "test-admin". The Fernet key + JWT secret are non-secret
 * test-only values; never use them in production.
 */

const REPO_ROOT = path.resolve(__dirname, "../..");
const BACKEND_DIR = path.resolve(REPO_ROOT, "web/backend");
const E2E_DATA_DIR = path.resolve(__dirname, ".pw-data");
const E2E_DB_PATH = path.resolve(__dirname, ".pw-data/e2e.db");

const ADMIN_USERNAME = "test-admin";
// bcrypt hash of "password" — produced via passlib.hash.bcrypt.
const ADMIN_PASSWORD_HASH =
  "$2b$12$cZ9ZjD2vAFt/rYJR8Ltdq.qKGDcveqHr3e.RshzlmbOd9g7MsTQcq";
const JWT_SECRET = "playwright-e2e-jwt-secret-do-not-use-in-prod";
// Valid Fernet key (base64-url 32 bytes) — test-only.
const FERNET_KEY = "qWRIk0Ulj9CHt_iUrhe4AVe31sK9LmbPT1MQICRwPco=";

const BACKEND_ENV = {
  ADMIN_USERNAME,
  ADMIN_PASSWORD_HASH,
  JWT_SECRET,
  FERNET_KEY,
  DATABASE_URL: `sqlite+aiosqlite:///${E2E_DB_PATH.replace(/\\/g, "/")}`,
  FAKE_LLM: "1",
  DATA_DIR: E2E_DATA_DIR,
  APP_ENV: "test",
  DEBUG: "1",
};

// Single shell-line that primes the DB then launches uvicorn.
// We avoid platform-specific mkdir (Windows `if not exist` parses only
// in cmd.exe; bash/sh trip on it — and Playwright's spawn uses whichever
// shell happens to be on PATH). The .pw-data dir is created up-front via
// `globalSetup` so the shell line below is purely cross-platform.
const backendCommand = [
  `alembic upgrade head`,
  `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`,
].join(" && ");

const skipWebServer = process.env.PW_SKIP_WEBSERVER === "1";

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/globalSetup.ts",
  fullyParallel: false, // backend has a single global run lock — serialise.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? "line" : "list",
  timeout: 60_000,
  use: {
    baseURL: process.env.PW_BASE_URL ?? "http://localhost:5173",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: skipWebServer
    ? undefined
    : [
        {
          command: backendCommand,
          cwd: BACKEND_DIR,
          // Health probe is exposed by the bootstrap router.
          url: "http://127.0.0.1:8000/api/health",
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
          env: BACKEND_ENV,
        },
        {
          command: "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort",
          cwd: __dirname,
          url: "http://127.0.0.1:5173",
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
          stdout: "pipe",
          stderr: "pipe",
        },
      ],
});
