import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * STUB. Downstream renders two cards:
 *   - API Keys: one masked row per provider_env (13 entries).
 *   - Defaults: mirrors NewRun form fields, persisted via PUT /settings/defaults.
 */
export default function Settings() {
  return (
    <div className="container space-y-6 py-8">
      <Card>
        <CardHeader>
          <CardTitle>API keys</CardTitle>
          <CardDescription>
            Stub. Masked per-provider rows (configured / not configured) with
            save and clear actions.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <code className="font-mono">GET/PUT/DELETE /api/settings/api-keys</code>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Defaults</CardTitle>
          <CardDescription>
            Stub. Pre-fill values for the new-run form.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          <code className="font-mono">GET/PUT /api/settings/defaults</code>
        </CardContent>
      </Card>
    </div>
  );
}
