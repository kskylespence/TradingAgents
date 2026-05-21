import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * STUB. The downstream "frontend-routes" agent will replace this with
 * a controlled form driven by useCatalog() that posts RunRequest to
 * /api/runs and navigates to /runs/:id.
 */
export default function NewRun() {
  return (
    <div className="container py-8">
      <Card>
        <CardHeader>
          <CardTitle>New analysis</CardTitle>
          <CardDescription>
            Stub. The submit form will live here (ticker, date, analysts,
            depth, provider, models, language, checkpoint).
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Driven by <code className="font-mono">/api/catalog/*</code> and
          posts <code className="font-mono">RunRequest</code> to{" "}
          <code className="font-mono">/api/runs</code>.
        </CardContent>
      </Card>
    </div>
  );
}
