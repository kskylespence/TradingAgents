import type { OllamaHealth } from "@/lib/types";

/**
 * Inline warning on NewRun when the selected provider is Ollama and
 * the backend's /api/health probe reports the upstream is down.
 *
 * Visibility logic (kept here, not in NewRun.tsx, so it can be unit-
 * tested without mounting the full form):
 *
 *   show iff provider === "ollama" AND health.ollama?.status === "down"
 *
 * "unknown" (no probe yet) and "ok" (including ok-with-zero-models)
 * both suppress the warning to avoid alert fatigue — a freshly-loaded
 * form shouldn't flash a destructive banner before the probe has even
 * had a chance to report. The 30s polling cadence on useHealth means
 * the alert appears within ~30s of upstream going down without
 * requiring a page reload.
 *
 * Note: `role="alert"` causes assistive tech to announce the message
 * on mount. That's the intended UX for a "you're about to do something
 * that won't work" warning — it interrupts other content, which is
 * exactly what we want.
 */
export function OllamaUpstreamAlert({
  provider,
  health,
}: {
  provider: string;
  health: OllamaHealth | null | undefined;
}) {
  const visible = provider === "ollama" && health?.status === "down";
  if (!visible) return null;

  const errorText = health?.error
    ? `Last error: ${health.error}`
    : "The backend's last probe to OLLAMA_BASE_URL failed.";

  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      <div className="font-medium">Ollama upstream is unreachable.</div>
      <div className="mt-1 text-xs text-destructive/90">
        {errorText} Submitting a run now will fail when the first agent
        calls the LLM. Check the backend's OLLAMA_BASE_URL /
        OLLAMA_API_KEY env vars before submitting.
      </div>
    </div>
  );
}
