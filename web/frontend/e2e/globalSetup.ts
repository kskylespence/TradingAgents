import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * Cross-platform pre-flight: ensure the per-run data directory exists
 * before Playwright spawns the backend webServer. The webServer command
 * itself is then purely cross-platform (no `if not exist` / `mkdir -p`
 * incantations that depend on which shell child_process.spawn picks).
 */
export default async function globalSetup(): Promise<void> {
  const dataDir = path.resolve(__dirname, "..", ".pw-data");
  mkdirSync(dataDir, { recursive: true });
}
