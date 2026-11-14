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
 * Audience: `audience="user"` is for standard (non-admin) users. They
 * can't pick a model or change env vars, so they get the same states
 * in plain words with no model ids, alternatives or config advice.
 * NewRun must render this for EVERY user: when it sat inside the
 * admin-only block, a refused run showed a standard user nothing.
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
  audience = "admin",
}: {
  provider: string;
  /** "user" swaps every message for plain, action-free wording. */
  audience?: "admin" | "user";
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
    return audience === "user" ? (
      <UserValidationAlert validation={validation} />
    ) : (
      <ValidationAlert validation={validation} />
    );
  }

  // Circuit-breaker state (v0.2.5+hf.4) takes precedence over the bare
  // status field — it tells us WHY the system is in the state it's in
  // and lets the UI render three calibrated states instead of binary
  // "down / not-down".
  const circuitState = health?.circuit_state ?? "closed";

  if (circuitState === "half_open") {
    if (audience === "user") {
      return (
        <UserNotice
          tone="status"
          title="The AI service is recovering."
          body="New analyses may work again in a moment."
        />
      );
    }
    // Yellow "recovering" pill — the breaker is letting one trial
    // probe through. The user shouldn't see a red alert; we're already
    // probing on their behalf.
    return (
      <div
        role="status"
        className="rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-sm text-amber-700 dark:text-amber-300"
      >
        <div className="font-medium">Recovering — upstream cooling down.</div>
        <div className="mt-1 text-xs">
          The backend is probing Ollama Cloud after a brief outage.
          New runs may succeed shortly.
        </div>
      </div>
    );
  }

  if (circuitState === "open") {
    if (audience === "user") {
      return (
        <UserNotice
          title="The AI service is having trouble."
          body="New analyses will fail for about the next minute. Try again shortly."
        />
      );
    }
    // Red cool-down notice — the breaker is open, requests will
    // short-circuit until the cooldown elapses. Tell the user
    // explicitly so they don't blame their config.
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
      >
        <div className="font-medium">
          Upstream in cool-down — new runs will fail.
        </div>
        <div className="mt-1 text-xs text-destructive/90">
          The shared HTTP client opened its circuit breaker after
          sustained Ollama Cloud failures. It will retry automatically
          after ~30 seconds. Wait, or switch to a different provider.
        </div>
      </div>
    );
  }

  // Steady-state: breaker is closed. Only fire the red alert when the
  // status is sustained-down (hysteresis already debounced single
  // transients on the backend side).
  if (health?.status !== "down") return null;

  if (audience === "user") {
    return (
      <UserNotice
        title="The AI service is unreachable right now."
        body="New analyses will fail. Try again later, and tell an admin if it lasts."
      />
    );
  }

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
  rate_limited: "hit the Ollama Cloud usage limit",
  degraded_empty_response: "returned an empty completion",
};

function isUsageLimit(validation: RunValidationError): boolean {
  return validation.unhealthy_models.some((m) => m.status === "rate_limited");
}

function UserNotice({
  title,
  body,
  tone = "alert",
}: {
  title: string;
  body: string;
  tone?: "alert" | "status";
}) {
  const colors =
    tone === "status"
      ? "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-300"
      : "border-destructive/50 bg-destructive/10 text-destructive";
  return (
    <div role={tone} className={`rounded-md border px-3 py-2 text-sm ${colors}`}>
      <div className="font-medium">{title}</div>
      <div className="mt-1 text-xs">{body}</div>
    </div>
  );
}

function UserValidationAlert({
  validation,
}: {
  validation: RunValidationError;
}) {
  return isUsageLimit(validation) ? (
    <UserNotice
      title="The AI service has hit its usage limit."
      body="Your analysis was not started. No analysis can run until the limit resets. Let an admin know, or try again later."
    />
  ) : (
    <UserNotice
      title="The AI models are not answering right now."
      body="Your analysis was not started. Try again in a few minutes, and tell an admin if it keeps happening."
    />
  );
}

function ValidationAlert({ validation }: { validation: RunValidationError }) {
  const { unhealthy_models, suggested_alternatives, message } = validation;
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive"
    >
      <div className="font-medium">
        {isUsageLimit(validation)
          ? "Ollama Cloud usage limit reached."
          : "Selected Ollama model is not responding."}
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
