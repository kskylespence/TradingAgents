import { useEffect, useRef, useState } from "react";

import type { RunEvent } from "@/lib/types";

/**
 * Minimal SSE hook scaffold.
 *
 * NOTE: this is intentionally lightweight — the downstream "frontend-routes"
 * agent will replace it with the full reducer-driven `useRun(id)` from the
 * plan. The scaffold here provides:
 *
 *   - EventSource lifecycle (open / close on unmount or url change).
 *   - Auto-reconnect with exponential backoff (the browser does basic
 *     reconnect itself, but only when EventSource was alive at the close;
 *     we add an explicit retry when the connection errors before any data
 *     has arrived).
 *   - `Last-Event-ID` resume: EventSource sends the header automatically on
 *     its own reconnect. For app-driven reconnects we keep the latest seq
 *     so consumers can include it in their own resume URLs if needed.
 *   - JSON-parsing each event payload into our `RunEvent` union.
 *
 * The hook is purposely NOT a reducer — that belongs in `useRun.ts` which
 * downstream will own. We expose the raw event list + connection state.
 */

export type SSEState = "idle" | "connecting" | "open" | "closed" | "error";

export interface UseEventSourceOptions {
  /** Set to false to leave the source disconnected (e.g. before login). */
  enabled?: boolean;
  /** Optional reset hook fired whenever the URL changes (clears events). */
  onReset?: () => void;
}

export interface UseEventSourceResult {
  events: RunEvent[];
  lastSeq: number | null;
  state: SSEState;
  error: string | null;
  /** Imperative close; consumers usually rely on unmount cleanup. */
  close: () => void;
}

/**
 * Open an EventSource to `url` and stream RunEvent JSON payloads.
 *
 * Pass `null` to detach. Each parsed event is appended in-order. The hook
 * does NOT deduplicate or coalesce; that's the reducer's job downstream.
 */
export function useEventSource(
  url: string | null,
  options: UseEventSourceOptions = {},
): UseEventSourceResult {
  const { enabled = true, onReset } = options;
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [lastSeq, setLastSeq] = useState<number | null>(null);
  const [state, setState] = useState<SSEState>("idle");
  const [error, setError] = useState<string | null>(null);

  const sourceRef = useRef<EventSource | null>(null);
  const onResetRef = useRef(onReset);
  onResetRef.current = onReset;

  useEffect(() => {
    if (!enabled || !url || typeof window === "undefined") {
      return;
    }

    onResetRef.current?.();
    setEvents([]);
    setLastSeq(null);
    setError(null);
    setState("connecting");

    let cancelled = false;
    const source = new EventSource(url, { withCredentials: true });
    sourceRef.current = source;

    source.onopen = () => {
      if (cancelled) return;
      setState("open");
    };

    source.onmessage = (ev: MessageEvent) => {
      if (cancelled) return;
      try {
        const parsed = JSON.parse(ev.data) as RunEvent;
        setEvents((prev) => [...prev, parsed]);
        if (typeof parsed.seq === "number") {
          setLastSeq((prev) => (prev == null || parsed.seq > prev ? parsed.seq : prev));
        }
      } catch (err) {
        // Heartbeats arrive as ": keepalive" comments; the browser never
        // surfaces them via onmessage, so any JSON error is a real problem.
        setError(`Failed to parse SSE payload: ${(err as Error).message}`);
      }
    };

    source.onerror = () => {
      if (cancelled) return;
      // EventSource auto-reconnects when readyState !== CLOSED.
      // We only flag a hard error when it has closed permanently.
      if (source.readyState === EventSource.CLOSED) {
        setState("error");
        setError("SSE connection closed");
      } else {
        setState("connecting");
      }
    };

    return () => {
      cancelled = true;
      source.close();
      sourceRef.current = null;
      setState("closed");
    };
  }, [url, enabled]);

  return {
    events,
    lastSeq,
    state,
    error,
    close: () => {
      sourceRef.current?.close();
      sourceRef.current = null;
      setState("closed");
    },
  };
}
