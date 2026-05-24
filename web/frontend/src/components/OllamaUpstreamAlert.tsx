import type { OllamaHealth, RunValidationError } from "@/lib/types";

/**
 * Inline warning on NewRun for two distinct Ollama failure modes:
 *
 * 1. **Steady-state health probe** — the background `/api/health` poll
 *    reports `ollama.status === "down"`. This is the original Layer-2
 *    behaviour: the server itself isn't reaching upstream at all.
 *
 * 2. **Pre-flight model probe (Layer 1)** — the user attempted to
 *    submit a run but the synchronous probe inside `POST /api/runs`
 *    found one of the selected models unresponsive. The backend
 *    returns HTTP 400 with a `RunValidationError` body; the parent
 *    passes it in here via the `validation` prop.
 *
 * Visibility logic (kept here, not in NewRun.tsx, so the rules can be
 * unit-tested without mounting the full form):
 *
 *   - provider must be "ollama" (both modes — non-Ollama providers
 *     never produce these signals today)
 *   - validation: present → render the per-model probe failure detail
 *   - else health.status === "down" → render the steady-state notice
 *   - otherwise → render nothing (avoid alert fatigue)
 *
 * Note: `role="alert"` causes assistive tech to announce the message
 * on mount. That's the intended UX for a "you're about to do something
 * that won't work" warning — it interrupts other content, which is
 * exactly what we want.
 */
export function OllamaUpstreamAlert({
  provider,
  health,
  validation,
}: {
  provider: string;
  health: OllamaHealth | null | undefined;
  /**
   * Populated when the most recent `POST /api/runs` attempt came back
   * with HTTP 400 + `code=upstream_model_unhealthy`. `null` clears the
   * banner (e.g. user changed model selection).
   */
  validation?: RunValidationError | null;
}) {
  if (provider !== "ollama") return null;

  if (validation) {
    return <ValidationAlert validation={validation} />;
  }

  if (health?.status !== "down") return null;

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

const STATUS_LABEL: Record<string, string> = {
  timeout: "timed out",
  http_5xx: "returned 5xx",
  http_4xx: "returned 4xx",
  degraded_empty_response: "returned an empty completion",
};

function ValidationAlert({ validation }: { validation: RunValidationError }) {
  const { unhealthy_models, suggested_alternatives, message } = validation;
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      <div className="font-medium">
        Selected Ollama model is not responding.
      </div>
      <div className="mt-1 text-xs text-destructive/90">{message}</div>

      <ul className="mt-2 space-y-1 text-xs text-destructive">
        {unhealthy_models.map((m) => (
          <li key={m.model} className="font-mono">
            <span className="font-semibold">{m.model}</span> —{" "}
            {STATUS_LABEL[m.status] ?? m.status}
            {m.upstream_ref ? (
              <span className="text-destructive/80"> (ref: {m.upstream_ref})</span>
            ) : null}
          </li>
        ))}
      </ul>

      {suggested_alternatives.length > 0 && (
        <div className="mt-2 text-xs text-destructive/90">
          <span className="font-medium">Known-good alternatives: </span>
          {suggested_alternatives.map((id, i) => (
            <span key={id} className="font-mono">
              {id}
              {i < suggested_alternatives.length - 1 ? ", " : ""}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
