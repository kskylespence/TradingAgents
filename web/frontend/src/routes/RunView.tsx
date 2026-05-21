import { useParams } from "react-router-dom";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * STUB. Downstream renders the CLI-style live layout
 * (header / progress grid / message log / report) driven by an SSE
 * reducer over /api/runs/:id/events.
 */
export default function RunView() {
  const { runId } = useParams<{ runId: string }>();
  return (
    <div className="container py-8">
      <Card>
        <CardHeader>
          <CardTitle>Run {runId}</CardTitle>
          <CardDescription>
            Stub. The live run view will live here.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Reads <code className="font-mono">GET /api/runs/{runId}</code> and
          streams <code className="font-mono">GET /api/runs/{runId}/events</code>.
        </CardContent>
      </Card>
    </div>
  );
}
