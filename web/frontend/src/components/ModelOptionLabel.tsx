import type { CatalogModel } from "@/lib/types";

/**
 * Per-option label rendering for the quick/deep model dropdowns in
 * NewRun. Extracted from NewRun.tsx so the badge logic can be unit-
 * tested without mounting the full form (jsdom hangs on the live
 * NewRun render — see the docstring in `NewRun.ollamaAlert.test.tsx`
 * for the same workaround on the Ollama upstream warning).
 *
 * Visibility logic
 * ----------------
 * Show the WARN badge iff `model.curated === false`.
 *
 * `undefined` (field omitted by the backend — true for non-Ollama
 * providers and for older backend versions that pre-date the
 * curated-flag rollout) MUST NOT trigger the badge. We have no
 * quality signal there, and retroactively badging static-catalog
 * providers (openai, anthropic, ...) would be misleading.
 *
 * `true` is the explicit "this model is in the curated cloud catalog"
 * signal and also suppresses the badge.
 *
 * The tooltip explains the risk so a user who notices the badge can
 * hover to learn why before picking a different model.
 */
export function ModelOptionLabel({ model }: { model: CatalogModel }) {
  const deprioritized = model.curated === false;
  return (
    <span className="inline-flex items-center gap-1">
      {deprioritized && (
        <span
          data-testid="deprioritized-badge"
          title={
            "Not in Ollama's curated cloud catalog. May have reliability " +
            "issues — consider glm-5.2, kimi-k2.6, or glm-5.1."
          }
          aria-label="Deprioritized model — may have reliability issues"
          className="text-amber-600"
        >
          ⚠
        </span>
      )}
      <span>{model.label}</span>
    </span>
  );
}

/**
 * Stable sort that bubbles curated models to the top while preserving
 * the relative order within each bucket. The backend emits Ollama
 * models in the order the upstream API returned them, which is
 * meaningful (usually alphabetical-ish), so we don't want to shuffle
 * within the curated set.
 *
 * `undefined` is treated as curated — see `ModelOptionLabel` for the
 * back-compat rationale (older backends and non-Ollama providers
 * omit the field, and we don't want to retroactively deprioritise
 * everything).
 *
 * Does NOT mutate the input — callers can pass the React Query data
 * array directly without copy-then-sort boilerplate.
 */
export function sortCuratedFirst<T extends Pick<CatalogModel, "curated">>(
  models: readonly T[],
): T[] {
  const curated: T[] = [];
  const deprioritized: T[] = [];
  for (const m of models) {
    // undefined OR true => curated bucket; only explicit false bubbles
    // to the bottom.
    if (m.curated === false) {
      deprioritized.push(m);
    } else {
      curated.push(m);
    }
  }
  return [...curated, ...deprioritized];
}
