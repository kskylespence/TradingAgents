import { useEffect, useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import ProviderApiKeyForm from "@/components/ProviderApiKeyForm";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useAnalysts,
  useLanguages,
  useModels,
  useProviders,
} from "@/hooks/useCatalog";
import { useToast } from "@/hooks/use-toast";
import { api, ApiError } from "@/lib/api";
import type {
  AnalystKey,
  AnthropicEffort,
  ApiKeyStatus,
  GoogleThinkingLevel,
  OpenAIReasoningEffort,
  ResearchDepth,
  ThinkingConfig,
  UserDefaults,
} from "@/lib/types";

const API_KEYS_QUERY_KEY = ["api-keys"] as const;
const USER_DEFAULTS_QUERY_KEY = ["user-defaults"] as const;

const DEPTH_OPTIONS: { value: ResearchDepth; label: string }[] = [
  { value: 1, label: "Shallow (1)" },
  { value: 3, label: "Medium (3)" },
  { value: 5, label: "Deep (5)" },
];

// Sentinel option representing "no default set". Radix Select disallows
// an empty-string value, so we encode "unset" explicitly and translate at
// the API boundary.
const UNSET = "__unset__";

interface DefaultsFormState {
  llm_provider: string;
  quick_think_llm: string;
  deep_think_llm: string;
  research_depth: ResearchDepth | null;
  analysts: AnalystKey[];
  output_language: string;
  google_thinking_level: GoogleThinkingLevel | "";
  openai_reasoning_effort: OpenAIReasoningEffort | "";
  anthropic_effort: AnthropicEffort | "";
  enable_checkpoint: boolean;
}

function toForm(defaults: UserDefaults | undefined): DefaultsFormState {
  const t = defaults?.thinking_config ?? null;
  return {
    llm_provider: defaults?.llm_provider ?? "",
    quick_think_llm: defaults?.quick_think_llm ?? "",
    deep_think_llm: defaults?.deep_think_llm ?? "",
    research_depth: defaults?.research_depth ?? null,
    analysts: defaults?.analysts ?? [],
    output_language: defaults?.output_language ?? "",
    google_thinking_level: t?.google_thinking_level ?? "",
    openai_reasoning_effort: t?.openai_reasoning_effort ?? "",
    anthropic_effort: t?.anthropic_effort ?? "",
    enable_checkpoint: defaults?.enable_checkpoint ?? false,
  };
}

function toPayload(form: DefaultsFormState): Partial<UserDefaults> {
  const thinking: ThinkingConfig = {};
  if (form.google_thinking_level) {
    thinking.google_thinking_level = form.google_thinking_level;
  }
  if (form.openai_reasoning_effort) {
    thinking.openai_reasoning_effort = form.openai_reasoning_effort;
  }
  if (form.anthropic_effort) {
    thinking.anthropic_effort = form.anthropic_effort;
  }
  return {
    llm_provider: form.llm_provider || null,
    quick_think_llm: form.quick_think_llm || null,
    deep_think_llm: form.deep_think_llm || null,
    research_depth: form.research_depth,
    analysts: form.analysts.length ? form.analysts : null,
    output_language: form.output_language || null,
    thinking_config: Object.keys(thinking).length ? thinking : null,
    enable_checkpoint: form.enable_checkpoint,
  };
}

function formatLastUpdated(iso: string | null): string | null {
  if (!iso) return null;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return null;
  const diffMs = Date.now() - then;
  if (diffMs < 0) return "just now";
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day}d ago`;
  const mo = Math.floor(day / 30);
  if (mo < 12) return `${mo}mo ago`;
  const yr = Math.floor(mo / 12);
  return `${yr}y ago`;
}

function ApiKeysCard() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: API_KEYS_QUERY_KEY,
    queryFn: () => api.get<ApiKeyStatus[]>("/api/settings/api-keys"),
  });

  const subtitle = useMemo(() => {
    if (!data || data.length === 0) return null;
    const configured = data.filter((e) => e.configured);
    if (configured.length === 0) return "No keys configured yet.";
    const mostRecent = configured
      .map((e) => e.last_updated)
      .filter((d): d is string => !!d)
      .sort()
      .pop();
    const ago = formatLastUpdated(mostRecent ?? null);
    const word = configured.length === 1 ? "key" : "keys";
    return ago
      ? `${configured.length} ${word} configured (updated ${ago})`
      : `${configured.length} ${word} configured`;
  }, [data]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>API keys</CardTitle>
        <CardDescription>
          {subtitle ??
            "Store provider API keys server-side. Values are encrypted at rest and never returned in plaintext."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {isLoading ? (
          <div className="space-y-4" aria-label="Loading API keys">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="space-y-2">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-10 w-full" />
              </div>
            ))}
          </div>
        ) : isError ? (
          <p className="text-sm text-destructive">
            Failed to load API keys
            {error instanceof ApiError ? `: ${error.message}` : ""}.
          </p>
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No providers registered.
          </p>
        ) : (
          data.map((entry) => (
            <ProviderApiKeyForm key={entry.provider_env} entry={entry} />
          ))
        )}
      </CardContent>
    </Card>
  );
}

function DefaultsCard() {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const defaultsQuery = useQuery({
    queryKey: USER_DEFAULTS_QUERY_KEY,
    queryFn: () => api.get<UserDefaults>("/api/settings/defaults"),
  });

  const [form, setForm] = useState<DefaultsFormState>(() => toForm(undefined));

  // Re-seed the form whenever the server payload changes (initial fetch
  // and post-save refresh). Local edits are lost on refresh by design —
  // saving is the only way to persist.
  useEffect(() => {
    if (defaultsQuery.data) {
      setForm(toForm(defaultsQuery.data));
    }
  }, [defaultsQuery.data]);

  const providers = useProviders();
  const analysts = useAnalysts("stock");
  const languages = useLanguages();
  const quickModels = useModels(form.llm_provider || null, "quick");
  const deepModels = useModels(form.llm_provider || null, "deep");

  const saveMutation = useMutation({
    mutationFn: (payload: Partial<UserDefaults>) =>
      api.put<UserDefaults>("/api/settings/defaults", payload),
    onSuccess: async (next) => {
      toast({ title: "Defaults saved" });
      queryClient.setQueryData(USER_DEFAULTS_QUERY_KEY, next);
      await queryClient.invalidateQueries({
        queryKey: USER_DEFAULTS_QUERY_KEY,
      });
    },
    onError: (err) => {
      const message =
        err instanceof ApiError
          ? err.message
          : "Could not save defaults. Please try again.";
      toast({
        title: "Save failed",
        description: message,
        variant: "destructive",
      });
    },
  });

  const toggleAnalyst = (key: AnalystKey, checked: boolean) => {
    setForm((prev) => {
      const set = new Set(prev.analysts);
      if (checked) set.add(key);
      else set.delete(key);
      return { ...prev, analysts: Array.from(set) };
    });
  };

  const handleProviderChange = (value: string) => {
    const next = value === UNSET ? "" : value;
    setForm((prev) =>
      prev.llm_provider === next
        ? prev
        : {
            ...prev,
            llm_provider: next,
            // Models are provider-scoped; clear when the provider changes
            // so we don't ship a stale model id to a different vendor.
            quick_think_llm: "",
            deep_think_llm: "",
          },
    );
  };

  const handleSelect = <K extends keyof DefaultsFormState>(
    key: K,
    rawValue: string,
    transform?: (v: string) => DefaultsFormState[K],
  ) => {
    const cleared = rawValue === UNSET ? "" : rawValue;
    setForm((prev) => ({
      ...prev,
      [key]: transform ? transform(cleared) : (cleared as DefaultsFormState[K]),
    }));
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    saveMutation.mutate(toPayload(form));
  };

  if (defaultsQuery.isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Defaults</CardTitle>
          <CardDescription>
            Pre-fill values for the new-run form.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4" aria-label="Loading defaults">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="space-y-2">
              <Skeleton className="h-4 w-32" />
              <Skeleton className="h-10 w-full" />
            </div>
          ))}
        </CardContent>
      </Card>
    );
  }

  if (defaultsQuery.isError) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Defaults</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-destructive">
            Failed to load defaults
            {defaultsQuery.error instanceof ApiError
              ? `: ${defaultsQuery.error.message}`
              : ""}
            .
          </p>
        </CardContent>
      </Card>
    );
  }

  const updatedAgo = formatLastUpdated(defaultsQuery.data?.updated_at ?? null);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Defaults</CardTitle>
        <CardDescription>
          Pre-fill values for the new-run form.
          {updatedAgo ? ` Last updated ${updatedAgo}.` : ""}
        </CardDescription>
      </CardHeader>
      <form onSubmit={handleSubmit}>
        <CardContent className="space-y-5">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label htmlFor="defaults-provider">LLM provider</Label>
              <Select
                value={form.llm_provider || UNSET}
                onValueChange={handleProviderChange}
                disabled={providers.isLoading}
              >
                <SelectTrigger id="defaults-provider">
                  <SelectValue placeholder="Select a provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={UNSET}>No default</SelectItem>
                  {(providers.data ?? []).map((p) => (
                    <SelectItem key={p.key} value={p.key}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="defaults-language">Output language</Label>
              <Select
                value={form.output_language || UNSET}
                onValueChange={(v) => handleSelect("output_language", v)}
                disabled={languages.isLoading}
              >
                <SelectTrigger id="defaults-language">
                  <SelectValue placeholder="Select a language" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={UNSET}>No default</SelectItem>
                  {(languages.data ?? []).map((l) => (
                    <SelectItem key={l.key} value={l.key}>
                      {l.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="defaults-quick">Quick model</Label>
              {quickModels.data && quickModels.data.length > 0 ? (
                <Select
                  value={form.quick_think_llm || UNSET}
                  onValueChange={(v) => handleSelect("quick_think_llm", v)}
                  disabled={!form.llm_provider || quickModels.isLoading}
                >
                  <SelectTrigger id="defaults-quick">
                    <SelectValue placeholder="Select a model" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>No default</SelectItem>
                    {quickModels.data.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id="defaults-quick"
                  placeholder={
                    form.llm_provider
                      ? "Model id"
                      : "Pick a provider first"
                  }
                  value={form.quick_think_llm}
                  disabled={!form.llm_provider}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, quick_think_llm: e.target.value }))
                  }
                />
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="defaults-deep">Deep model</Label>
              {deepModels.data && deepModels.data.length > 0 ? (
                <Select
                  value={form.deep_think_llm || UNSET}
                  onValueChange={(v) => handleSelect("deep_think_llm", v)}
                  disabled={!form.llm_provider || deepModels.isLoading}
                >
                  <SelectTrigger id="defaults-deep">
                    <SelectValue placeholder="Select a model" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>No default</SelectItem>
                    {deepModels.data.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id="defaults-deep"
                  placeholder={
                    form.llm_provider
                      ? "Model id"
                      : "Pick a provider first"
                  }
                  value={form.deep_think_llm}
                  disabled={!form.llm_provider}
                  onChange={(e) =>
                    setForm((p) => ({ ...p, deep_think_llm: e.target.value }))
                  }
                />
              )}
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="defaults-depth">Research depth</Label>
              <Select
                value={form.research_depth ? String(form.research_depth) : UNSET}
                onValueChange={(v) =>
                  setForm((p) => ({
                    ...p,
                    research_depth:
                      v === UNSET ? null : (Number(v) as ResearchDepth),
                  }))
                }
              >
                <SelectTrigger id="defaults-depth">
                  <SelectValue placeholder="Select depth" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={UNSET}>No default</SelectItem>
                  {DEPTH_OPTIONS.map((d) => (
                    <SelectItem key={d.value} value={String(d.value)}>
                      {d.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="space-y-2">
            <Label>Analysts</Label>
            {analysts.isLoading ? (
              <Skeleton className="h-10 w-full" />
            ) : (
              <div className="grid gap-2 sm:grid-cols-2">
                {(analysts.data ?? []).map((a) => {
                  const id = `defaults-analyst-${a.key}`;
                  const checked = form.analysts.includes(a.key);
                  return (
                    <div key={a.key} className="flex items-center gap-2">
                      <Checkbox
                        id={id}
                        checked={checked}
                        onCheckedChange={(value) =>
                          toggleAnalyst(a.key, value === true)
                        }
                      />
                      <Label htmlFor={id} className="font-normal">
                        {a.label}
                      </Label>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <fieldset className="space-y-3 rounded-md border p-4">
            <legend className="px-1 text-sm font-medium">
              Reasoning effort
            </legend>
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="space-y-1.5">
                <Label htmlFor="defaults-openai-effort">OpenAI</Label>
                <Select
                  value={form.openai_reasoning_effort || UNSET}
                  onValueChange={(v) =>
                    handleSelect("openai_reasoning_effort", v, (val) =>
                      (val || "") as OpenAIReasoningEffort | "",
                    )
                  }
                >
                  <SelectTrigger id="defaults-openai-effort">
                    <SelectValue placeholder="Not set" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>Not set</SelectItem>
                    <SelectItem value="low">Low</SelectItem>
                    <SelectItem value="medium">Medium</SelectItem>
                    <SelectItem value="high">High</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="defaults-anthropic-effort">Anthropic</Label>
                <Select
                  value={form.anthropic_effort || UNSET}
                  onValueChange={(v) =>
                    handleSelect("anthropic_effort", v, (val) =>
                      (val || "") as AnthropicEffort | "",
                    )
                  }
                >
                  <SelectTrigger id="defaults-anthropic-effort">
                    <SelectValue placeholder="Not set" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>Not set</SelectItem>
                    <SelectItem value="low">Low</SelectItem>
                    <SelectItem value="medium">Medium</SelectItem>
                    <SelectItem value="high">High</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="defaults-google-effort">Google thinking</Label>
                <Select
                  value={form.google_thinking_level || UNSET}
                  onValueChange={(v) =>
                    handleSelect("google_thinking_level", v, (val) =>
                      (val || "") as GoogleThinkingLevel | "",
                    )
                  }
                >
                  <SelectTrigger id="defaults-google-effort">
                    <SelectValue placeholder="Not set" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={UNSET}>Not set</SelectItem>
                    <SelectItem value="minimal">Minimal</SelectItem>
                    <SelectItem value="high">High</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </fieldset>

          <div className="flex items-center gap-2">
            <Checkbox
              id="defaults-checkpoint"
              checked={form.enable_checkpoint}
              onCheckedChange={(value) =>
                setForm((p) => ({ ...p, enable_checkpoint: value === true }))
              }
            />
            <Label htmlFor="defaults-checkpoint" className="font-normal">
              Enable checkpoint / resume by default
            </Label>
          </div>
        </CardContent>
        <CardFooter className="flex justify-end gap-2">
          <Button
            type="button"
            variant="ghost"
            onClick={() => setForm(toForm(defaultsQuery.data))}
            disabled={saveMutation.isPending}
          >
            Revert
          </Button>
          <Button type="submit" disabled={saveMutation.isPending}>
            {saveMutation.isPending ? "Saving..." : "Save defaults"}
          </Button>
        </CardFooter>
      </form>
    </Card>
  );
}

export default function Settings() {
  return (
    <div className="container space-y-6 py-8">
      <ApiKeysCard />
      <DefaultsCard />
    </div>
  );
}
