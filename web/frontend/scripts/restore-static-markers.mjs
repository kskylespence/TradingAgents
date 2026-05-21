// Restores the `.gitkeep` and `.gitignore` markers in
// `web/backend/app/static/` after `vite build` empties the directory.
//
// Vite's `emptyOutDir: true` wipes the entire output directory; the static
// folder needs to keep its git markers so the directory survives a fresh
// checkout (the Dockerfile COPY at image-build time replaces them anyway).

import { writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(here, "../../backend/app/static");

writeFileSync(resolve(staticDir, ".gitkeep"), "");
writeFileSync(
  resolve(staticDir, ".gitignore"),
  [
    "# Build artifacts emitted by `web/frontend/npm run build` and copied in at",
    "# image-build time. We only commit `.gitkeep` (and this file) so the",
    "# directory exists in git.",
    "*",
    "!.gitignore",
    "!.gitkeep",
    "",
  ].join("\n"),
);
