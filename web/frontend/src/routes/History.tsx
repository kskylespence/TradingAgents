import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/**
 * STUB. Downstream renders a cursor-paginated table of /api/history.
 */
export default function History() {
  return (
    <div className="container py-8">
      <Card>
        <CardHeader>
          <CardTitle>History</CardTitle>
          <CardDescription>
            Stub. Cursor-paginated table of past runs (date, ticker, rating,
            depth, provider, elapsed).
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          Reads <code className="font-mono">GET /api/history</code>.
        </CardContent>
      </Card>
    </div>
  );
}
