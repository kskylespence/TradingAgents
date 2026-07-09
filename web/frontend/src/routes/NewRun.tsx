import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import {
  ModelOptionLabel,
  sortCuratedFirst,
} from "@/components/ModelOptionLabel";
import { OllamaUpstreamAlert } from "@/components/OllamaUpstreamAlert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Form,
  FormControl,
  FormDescription,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import {
  useAnalysts,
  useLanguages,
  useModels,
  useProviders,
} from "@/hooks/useCatalog";
import { useHealth } from "@/hooks/useHealth";
import { useUserDefaults } from "@/hooks/useUserDefaults";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { api, ApiError } from "@/lib/api";
import { inferAssetType } from "@/lib/assetType";
import type {
  AnalystKey,
  AnthropicEffort,
  AssetType,
  CatalogModel,
  GoogleThinkingLevel,
  OpenAIReasoningEffort,
  ResearchDepth,
  RunRequest,
  RunValidationError,
} from "@/lib/types";

const DEPTH_OPTIONS: { value: ResearchDepth; label: string; hint: string }[] = [
  { value: 1, label: "Shallow (1)", hint: "1 debate round — fastest" },
  { value: 3, label: "Medium (3)", hint: "3 debate rounds — balanced" },
  { value: 5, label: "Deep (5)", hint: "5 debate rounds — most thorough" },
];

const OPENAI_EFFORTS: OpenAIReasoningEffort[] = ["low", "medium", "high"];
const ANTHROPIC_EFFORTS: AnthropicEffort[] = ["low", "medium", "high"];
const GOOGLE_LEVELS: GoogleThinkingLevel[] = ["high", "minimal"];

const CUSTOM_MODEL_SENTINEL = "__custom__";

function todayLocalISODate(): string {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

interface RunCreatedResponse {
  run_id: string;
  status: string;
}

interface ConflictDetail {
  active_run_id?: string;
}

/**
 * /new — run-submission form. Mirrors the 8 CLI steps in `cli/main.py`:
 *
 *   1. Ticker  2. Analysis date  3. Analysts  4. Research depth
 *   5. Provider  6. Quick-think model  7. Deep-think model
 *   8. Output language
 *
 * Plus the optional thinking-config knob (provider-specific) and the
 * checkpoint toggle. Catalog data is sourced live from `/api/catalog/*`
 * so adding a new provider/model/language backend-side flows here for
 * free with no UI change.
 *
 * On submit: POST `RunRequest` to `/api/runs` (CSRF + auth handled by
 * `api.ts`) and navigate to `/runs/:run_id`. 409 means another run is
 * already in progress — we link to it from the toast.
 */
export default function NewRun() {
  const navigate = useNavigate();
  const { toast } = useToast();
  const isAdmin = useIsAdmin();

  // --- Form state --------------------------------------------------------- //
  const [ticker, setTicker] = useState("");
  // We debounce the ticker before deriving asset_type so we don't burn
  // queries on every keystroke (the analyst catalog is keyed by it).
  const [debouncedTicker, setDebouncedTicker] = useState("");
  const [analysisDate, setAnalysisDate] = useState(todayLocalISODate());
  const [selectedAnalysts, setSelectedAnalysts] = useState<Set<AnalystKey>>(
    new Set(),
  );
  const [researchDepth, setResearchDepth] = useState<ResearchDepth>(1);
  const [provider, setProvider] = useState<string>("");
  const [quickModelSel, setQuickModelSel] = useState<string>("");
  const [quickModelCustom, setQuickModelCustom] = useState<string>("");
  const [deepModelSel, setDeepModelSel] = useState<string>("");
  const [deepModelCustom, setDeepModelCustom] = useState<string>("");
  const [language, setLanguage] = useState<string>("");
  const [openaiEffort, setOpenaiEffort] =
    useState<OpenAIReasoningEffort>("medium");
  const [anthropicEffort, setAnthropicEffort] =
    useState<AnthropicEffort>("medium");
  const [googleLevel, setGoogleLevel] = useState<GoogleThinkingLevel>("high");
  const [enableCheckpoint, setEnableCheckpoint] = useState(true);
  const [validationError, setValidationError] = useState<string | null>(null);
  // 400 detail from `POST /api/runs` when the pre-flight liveness probe
  // (web/backend Layer 1) rejects a selected Ollama model. Surfaced via
  // the same `OllamaUpstreamAlert` block used for the steady-state
  // health probe.
  const [probeValidation, setProbeValidation] = useState<
    RunValidationError | null
  >(null);

  // --- Debounce ticker → asset_type → analyst filter ----------------------- //
  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedTicker(ticker), 300);
    return () => window.clearTimeout(handle);
  }, [ticker]);

  const assetType: AssetType = useMemo(
    () => inferAssetType(debouncedTicker),
    [debouncedTicker],
  );

  // --- Catalog queries ---------------------------------------------------- //
  const providersQuery = useProviders();
  const analystsQuery = useAnalysts(assetType);
  const languagesQuery = useLanguages();
  const quickModelsQuery = useModels(provider || null, "quick");
  const deepModelsQuery = useModels(provider || null, "deep");
  const defaultsQuery = useUserDefaults(isAdmin);
  // Polls /api/health every 30s. The OllamaUpstreamAlert component
  // below renders an inline warning when provider=ollama and the
  // upstream probe says "down", so the user gets the failure signal
  // before submit instead of an SSE-streamed engine error ~10s after.
  const healthQuery = useHealth();

  // --- Apply catalog defaults (only once each, when data first arrives) --- //
  useEffect(() => {
    if (provider) return;
    const fromDefaults = defaultsQuery.data?.llm_provider ?? null;
    const list = providersQuery.data;
    if (!list || list.length === 0) return;
    const initial =
      (fromDefaults && list.find((p) => p.key === fromDefaults)?.key) ||
      list[0].key;
    setProvider(initial);
  }, [provider, providersQuery.data, defaultsQuery.data]);

  useEffect(() => {
    if (language) return;
    const fromDefaults = defaultsQuery.data?.output_language ?? null;
    const list = languagesQuery.data;
    if (!list || list.length === 0) return;
    const english = list.find((l) => l.key.toLowerCase() === "english");
    const initial =
      (fromDefaults && list.find((l) => l.key === fromDefaults)?.key) ||
      english?.key ||
      list[0].key;
    setLanguage(initial);
  }, [language, languagesQuery.data, defaultsQuery.data]);

  // Reset model selections when provider changes; preserve saved-default
  // value if it appears in the new provider's catalog.
  useEffect(() => {
    setQuickModelSel("");
    setQuickModelCustom("");
    setDeepModelSel("");
    setDeepModelCustom("");
    // A probe failure is tied to the previous selection — switching
    // provider or model dismisses the banner so a stale warning doesn't
    // linger after the user has already corrected the choice.
    setProbeValidation(null);
  }, [provider]);

  useEffect(() => {
    setProbeValidation(null);
  }, [quickModelSel, deepModelSel]);

  useEffect(() => {
    if (quickModelSel) return;
    const list = quickModelsQuery.data;
    if (!list || list.length === 0) return;
    const saved = defaultsQuery.data?.quick_think_llm ?? null;
    const match = saved ? list.find((m) => m.id === saved) : null;
    setQuickModelSel(match ? match.id : list[0].id);
  }, [quickModelSel, quickModelsQuery.data, defaultsQuery.data]);

  useEffect(() => {
    if (deepModelSel) return;
    const list = deepModelsQuery.data;
    if (!list || list.length === 0) return;
    const saved = defaultsQuery.data?.deep_think_llm ?? null;
    const match = saved ? list.find((m) => m.id === saved) : null;
    setDeepModelSel(match ? match.id : list[0].id);
  }, [deepModelSel, deepModelsQuery.data, defaultsQuery.data]);

  // Apply analyst + depth + checkpoint defaults once.
  const [appliedScalarDefaults, setAppliedScalarDefaults] = useState(false);
  useEffect(() => {
    if (appliedScalarDefaults) return;
    const d = defaultsQuery.data;
    if (!d) return;
    if (d.research_depth) setResearchDepth(d.research_depth);
    // enable_checkpoint default is true on the schema; respect explicit false.
    setEnableCheckpoint(d.enable_checkpoint);
    setAppliedScalarDefaults(true);
  }, [appliedScalarDefaults, defaultsQuery.data]);

  // Seed analyst selection from defaults intersected with the current
  // (asset-type-filtered) catalog. If nothing matches, pick all available.
  const [analystsSeeded, setAnalystsSeeded] = useState(false);
  useEffect(() => {
    if (analystsSeeded) return;
    const list = analystsQuery.data;
    if (!list) return;
    const available = new Set(list.map((a) => a.key));
    const fromDefaults = defaultsQuery.data?.analysts ?? null;
    let seed: AnalystKey[];
    if (fromDefaults && fromDefaults.length > 0) {
      seed = fromDefaults.filter((k) => available.has(k));
      if (seed.length === 0) seed = list.map((a) => a.key);
    } else {
      seed = list.map((a) => a.key);
    }
    setSelectedAnalysts(new Set(seed));
    setAnalystsSeeded(true);
  }, [analystsSeeded, analystsQuery.data, defaultsQuery.data]);

  // When the asset-type-filtered analyst list changes (e.g. switching
  // AAPL → BTC-USD), drop selections that are no longer valid.
  useEffect(() => {
    const list = analystsQuery.data;
    if (!list) return;
    const available = new Set(list.map((a) => a.key));
    setSelectedAnalysts((prev) => {
      const next = new Set<AnalystKey>();
      for (const k of prev) if (available.has(k)) next.add(k);
      // If the filter wiped everything out, default to all available so the
      // user isn't stuck with a submit button they can't click.
      if (next.size === 0 && analystsSeeded) {
        for (const a of list) next.add(a.key);
      }
      return next;
    });
  }, [analystsQuery.data, analystsSeeded]);

  // --- Derived custom-model state ---------------------------------------- //
  const quickAllowsCustom = useMemo(() => {
    const list = quickModelsQuery.data;
    if (!list) return false;
    const entry = list.find((m) => m.id === quickModelSel);
    return !!entry?.allows_custom;
  }, [quickModelsQuery.data, quickModelSel]);

  const deepAllowsCustom = useMemo(() => {
    const list = deepModelsQuery.data;
    if (!list) return false;
    const entry = list.find((m) => m.id === deepModelSel);
    return !!entry?.allows_custom;
  }, [deepModelsQuery.data, deepModelSel]);

  const effectiveQuickModel = quickAllowsCustom
    ? quickModelCustom.trim()
    : quickModelSel;
  const effectiveDeepModel = deepAllowsCustom
    ? deepModelCustom.trim()
    : deepModelSel;

  // --- Submit mutation ---------------------------------------------------- //
  const mutation = useMutation<RunCreatedResponse, ApiError, RunRequest>({
    mutationFn: (body) => api.post<RunCreatedResponse>("/api/runs", body),
    onSuccess: (data) => {
      setProbeValidation(null);
      navigate(`/runs/${data.run_id}`);
    },
    onError: (err) => {
      if (err.status === 409) {
        const detail = err.body as ConflictDetail | null;
        const activeId = detail?.active_run_id;
        toast({
          title: "Another run is in progress",
          description: activeId ? (
            <span>
              See it{" "}
              <Link to={`/runs/${activeId}`} className="underline">
                here
              </Link>
              .
            </span>
          ) : (
            "Wait for it to finish, then try again."
          ),
          variant: "destructive",
        });
      } else if (err.status === 400) {
        // Pre-flight liveness probe rejection — backend Layer 1.
        // The 400 body is `{ detail: RunValidationError }`; surface it
        // inline via the alert rather than a transient toast so the
        // user can actually act on the alternatives list.
        const detail = (err.body as { detail?: unknown } | null)?.detail;
        if (
          detail &&
          typeof detail === "object" &&
          (detail as RunValidationError).code === "upstream_model_unhealthy"
        ) {
          setProbeValidation(detail as RunValidationError);
        } else {
          toast({
            title: "Could not start run",
            description: err.message,
            variant: "destructive",
          });
        }
      } else if (err.status === 422 || (err.status >= 400 && err.status < 500)) {
        toast({
          title: "Could not start run",
          description: err.message,
          variant: "destructive",
        });
      } else {
        toast({
          title: "Server error",
          description: err.message,
          variant: "destructive",
        });
      }
    },
  });

  // --- Validation + submit ------------------------------------------------ //
  function validate(): string | null {
    const trimmedTicker = ticker.trim();
    if (!trimmedTicker) return "Ticker is required.";
    if (!analysisDate) return "Analysis date is required.";
    // Compare as YYYY-MM-DD strings — lexicographic ordering matches calendar
    // ordering and avoids cross-timezone Date() footguns.
    if (analysisDate > todayLocalISODate()) {
      return "Analysis date can't be in the future.";
    }
    if (selectedAnalysts.size === 0) {
      return "Select at least one analyst.";
    }
    if (!isAdmin) return null;
    if (!provider) return "Pick an LLM provider.";
    if (!effectiveQuickModel) {
      return quickAllowsCustom
        ? "Enter a custom quick-think model ID."
        : "Pick a quick-think model.";
    }
    if (!effectiveDeepModel) {
      return deepAllowsCustom
        ? "Enter a custom deep-think model ID."
        : "Pick a deep-think model.";
    }
    if (!language) return "Pick an output language.";
    return null;
  }

  function buildRequest(): RunRequest {
    const body: RunRequest = {
      ticker: ticker.trim(),
      analysis_date: analysisDate,
      output_language: isAdmin ? language : "English",
      analysts: (analystsQuery.data ?? [])
        .map((a) => a.key)
        .filter((k) => selectedAnalysts.has(k)),
      research_depth: researchDepth,
      llm_provider: isAdmin ? provider : "ollama",
      quick_think_llm: isAdmin ? effectiveQuickModel : "glm-5.2",
      deep_think_llm: isAdmin ? effectiveDeepModel : "glm-5.2",
      enable_checkpoint: isAdmin ? enableCheckpoint : true,
    };
    if (isAdmin && provider === "openai") body.openai_reasoning_effort = openaiEffort;
    if (isAdmin && provider === "anthropic") body.anthropic_effort = anthropicEffort;
    if (isAdmin && provider === "google") body.google_thinking_level = googleLevel;
    return body;
  }

  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const err = validate();
    setValidationError(err);
    if (err) return;
    mutation.mutate(buildRequest());
  }

  // --- Render helpers ----------------------------------------------------- //
  function toggleAnalyst(key: AnalystKey, checked: boolean) {
    setSelectedAnalysts((prev) => {
      const next = new Set(prev);
      if (checked) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  const isSubmitting = mutation.isPending;
  const todayStr = todayLocalISODate();

  return (
    <div className="container max-w-3xl py-8">
      <Card className="border-emerald-900/40 bg-card/80">
        <CardHeader>
          <CardTitle className="font-mono tracking-tight">New analysis</CardTitle>
          <CardDescription>
            Submit a ticker for multi-agent research. Configure models in Settings
            {isAdmin ? "" : " (admin only)"}.
          </CardDescription>
        </CardHeader>
        <Form onSubmit={onSubmit} noValidate>
          <CardContent className="space-y-6">
            {/* 1. Ticker */}
            <FormItem>
              <FormLabel htmlFor="ticker">Ticker</FormLabel>
              <FormControl>
                <Input
                  id="ticker"
                  name="ticker"
                  autoFocus
                  required
                  placeholder="AAPL, NVDA, BTC-USD, RELIANCE.NS"
                  value={ticker}
                  onChange={(e) => setTicker(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </FormControl>
              <FormDescription>
                Detected asset type:{" "}
                <span className="font-medium">{assetType}</span>. Crypto
                suffixes (-USD, -USDT, -BTC, ...) switch the analyst set
                automatically.
              </FormDescription>
            </FormItem>

            {/* 2. Analysis date */}
            <FormItem>
              <FormLabel htmlFor="analysis_date">Analysis date</FormLabel>
              <FormControl>
                <Input
                  id="analysis_date"
                  name="analysis_date"
                  type="date"
                  required
                  max={todayStr}
                  value={analysisDate}
                  onChange={(e) => setAnalysisDate(e.target.value)}
                />
              </FormControl>
            </FormItem>

            {/* 3. Analysts */}
            <FormItem>
              <FormLabel>Analysts</FormLabel>
              <FormDescription>
                At least one. Crypto tickers hide analysts the backend
                can't run on coins (e.g. fundamentals).
              </FormDescription>
              <FormControl>
                <div className="grid gap-2 sm:grid-cols-2">
                  {analystsQuery.isLoading && (
                    <span className="text-sm text-muted-foreground">
                      Loading analysts...
                    </span>
                  )}
                  {(analystsQuery.data ?? []).map((a) => {
                    const id = `analyst-${a.key}`;
                    return (
                      <label
                        key={a.key}
                        htmlFor={id}
                        className="flex cursor-pointer items-center gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm hover:bg-accent"
                      >
                        <Checkbox
                          id={id}
                          checked={selectedAnalysts.has(a.key)}
                          onCheckedChange={(v) =>
                            toggleAnalyst(a.key, v === true)
                          }
                        />
                        <span>{a.label}</span>
                      </label>
                    );
                  })}
                </div>
              </FormControl>
            </FormItem>

            {/* 4. Research depth */}
            <FormItem>
              <FormLabel htmlFor="research_depth">Research depth</FormLabel>
              <FormControl>
                <Select
                  value={String(researchDepth)}
                  onValueChange={(v) =>
                    setResearchDepth(Number(v) as ResearchDepth)
                  }
                >
                  <SelectTrigger id="research_depth">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {DEPTH_OPTIONS.map((o) => (
                      <SelectItem key={o.value} value={String(o.value)}>
                        {o.label} — {o.hint}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormControl>
            </FormItem>

            {isAdmin ? (
              <>
            {/* Ollama upstream warning — see OllamaUpstreamAlert for
                the visibility logic. Extracted so the alert can be
                unit-tested without mounting the full form. Renders
                two modes: steady-state health-probe failures (background
                /api/health poll) AND submit-time pre-flight model
                probe failures (RunValidationError from POST /api/runs). */}
            <OllamaUpstreamAlert
              provider={provider}
              health={healthQuery.data?.ollama ?? null}
              validation={probeValidation}
            />

            {/* 5. LLM provider */}
            <FormItem>
              <FormLabel htmlFor="provider">LLM provider</FormLabel>
              <FormControl>
                <Select
                  value={provider}
                  onValueChange={setProvider}
                  disabled={
                    providersQuery.isLoading || !providersQuery.data?.length
                  }
                >
                  <SelectTrigger id="provider">
                    <SelectValue
                      placeholder={
                        providersQuery.isLoading
                          ? "Loading..."
                          : "Pick a provider"
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {(providersQuery.data ?? []).map((p) => (
                      <SelectItem key={p.key} value={p.key}>
                        {p.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormControl>
            </FormItem>

            {/* 6. Quick-think model */}
            <ModelPicker
              id="quick_model"
              label="Quick-think model"
              models={quickModelsQuery.data}
              loading={quickModelsQuery.isLoading}
              selected={quickModelSel}
              onSelect={setQuickModelSel}
              custom={quickModelCustom}
              onCustomChange={setQuickModelCustom}
              allowsCustom={quickAllowsCustom}
            />

            {/* 7. Deep-think model */}
            <ModelPicker
              id="deep_model"
              label="Deep-think model"
              models={deepModelsQuery.data}
              loading={deepModelsQuery.isLoading}
              selected={deepModelSel}
              onSelect={setDeepModelSel}
              custom={deepModelCustom}
              onCustomChange={setDeepModelCustom}
              allowsCustom={deepAllowsCustom}
            />

            {/* 8. Output language */}
            <FormItem>
              <FormLabel htmlFor="language">Output language</FormLabel>
              <FormControl>
                <Select
                  value={language}
                  onValueChange={setLanguage}
                  disabled={
                    languagesQuery.isLoading || !languagesQuery.data?.length
                  }
                >
                  <SelectTrigger id="language">
                    <SelectValue placeholder="Pick a language" />
                  </SelectTrigger>
                  <SelectContent>
                    {(languagesQuery.data ?? []).map((l) => (
                      <SelectItem key={l.key} value={l.key}>
                        {l.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </FormControl>
            </FormItem>

            {/* 9. Provider-specific thinking config */}
            {provider === "openai" && (
              <FormItem>
                <FormLabel htmlFor="openai_effort">
                  OpenAI reasoning effort
                </FormLabel>
                <FormControl>
                  <Select
                    value={openaiEffort}
                    onValueChange={(v) =>
                      setOpenaiEffort(v as OpenAIReasoningEffort)
                    }
                  >
                    <SelectTrigger id="openai_effort">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {OPENAI_EFFORTS.map((e) => (
                        <SelectItem key={e} value={e}>
                          {e}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormControl>
              </FormItem>
            )}
            {provider === "anthropic" && (
              <FormItem>
                <FormLabel htmlFor="anthropic_effort">
                  Anthropic effort
                </FormLabel>
                <FormControl>
                  <Select
                    value={anthropicEffort}
                    onValueChange={(v) =>
                      setAnthropicEffort(v as AnthropicEffort)
                    }
                  >
                    <SelectTrigger id="anthropic_effort">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {ANTHROPIC_EFFORTS.map((e) => (
                        <SelectItem key={e} value={e}>
                          {e}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormControl>
              </FormItem>
            )}
            {provider === "google" && (
              <FormItem>
                <FormLabel htmlFor="google_level">
                  Google thinking level
                </FormLabel>
                <FormControl>
                  <Select
                    value={googleLevel}
                    onValueChange={(v) =>
                      setGoogleLevel(v as GoogleThinkingLevel)
                    }
                  >
                    <SelectTrigger id="google_level">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {GOOGLE_LEVELS.map((l) => (
                        <SelectItem key={l} value={l}>
                          {l}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </FormControl>
              </FormItem>
            )}

            {/* 10. Enable checkpoint */}
            <FormItem>
              <FormControl>
                <label
                  htmlFor="enable_checkpoint"
                  className="flex cursor-pointer items-start gap-3 rounded-md border border-input bg-background p-3 hover:bg-accent"
                >
                  <Checkbox
                    id="enable_checkpoint"
                    checked={enableCheckpoint}
                    onCheckedChange={(v) => setEnableCheckpoint(v === true)}
                  />
                  <span className="space-y-1 text-sm">
                    <span className="block font-medium leading-none">
                      Enable LangGraph checkpoint
                    </span>
                    <span className="block text-muted-foreground">
                      Persist intermediate state so a crashed run can resume
                      from the last successful node.
                    </span>
                  </span>
                </label>
              </FormControl>
            </FormItem>
              </>
            ) : null}

            <FormMessage>{validationError}</FormMessage>
          </CardContent>
          <CardFooter className="flex justify-end gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={() => navigate("/history")}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Starting..." : "Start analysis"}
            </Button>
          </CardFooter>
        </Form>
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// ModelPicker — shared between quick + deep selectors.                        //
// --------------------------------------------------------------------------- //

interface ModelPickerProps {
  id: string;
  label: string;
  models: CatalogModel[] | undefined;
  loading: boolean;
  selected: string;
  onSelect: (id: string) => void;
  custom: string;
  onCustomChange: (v: string) => void;
  allowsCustom: boolean;
}

function ModelPicker({
  id,
  label,
  models,
  loading,
  selected,
  onSelect,
  custom,
  onCustomChange,
  allowsCustom,
}: ModelPickerProps) {
  // Radix Select doesn't allow an empty string as an item value, and we
  // also need to render *something* selected even while we wait on the
  // catalog. Use a sentinel placeholder that's never set as a real value.
  const placeholder = loading ? "Loading models..." : "Pick a model";
  const value = selected || CUSTOM_MODEL_SENTINEL;

  return (
    <FormItem>
      <FormLabel htmlFor={id}>{label}</FormLabel>
      <FormControl>
        <div className="space-y-2">
          <Select
            value={value === CUSTOM_MODEL_SENTINEL ? undefined : value}
            onValueChange={onSelect}
            disabled={loading || !models?.length}
          >
            <SelectTrigger id={id}>
              <SelectValue placeholder={placeholder} />
            </SelectTrigger>
            <SelectContent>
              {sortCuratedFirst(models ?? []).map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  <ModelOptionLabel model={m} />
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {allowsCustom && (
            <Input
              placeholder="Custom model ID (e.g. gpt-4o-mini-2024-07-18)"
              value={custom}
              onChange={(e) => onCustomChange(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          )}
        </div>
      </FormControl>
      {allowsCustom && (
        <FormDescription>
          This catalog entry supports custom model IDs — the text you
          enter is sent verbatim to the provider.
        </FormDescription>
      )}
    </FormItem>
  );
}
