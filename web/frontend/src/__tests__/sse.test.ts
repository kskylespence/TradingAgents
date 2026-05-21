import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useEventSource } from "@/lib/sse";
import type { RunEvent } from "@/lib/types";

/**
 * SSE hook unit tests.
 *
 * Strategy: replace globalThis.EventSource with a controllable fake class
 * (FakeEventSource) before every test. Each test pushes lifecycle events
 * (open / message / error / close) by hand and asserts the hook's
 * observable state matches.
 *
 * We test BEHAVIOR (events surface to the consumer, state transitions,
 * cleanup) not implementation. The hook itself is imported as-is.
 */

// --------------------------------------------------------------------------- //
// Test double: a minimal EventSource that lets the test drive lifecycle.       //
// --------------------------------------------------------------------------- //

interface ConstructorCall {
  url: string | URL;
  withCredentials: boolean;
}

class FakeEventSource implements Partial<EventSource> {
  // Mirror the real readyState constants so the hook's
  // `source.readyState === EventSource.CLOSED` check works on the instance.
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  // Track every constructor invocation across the suite so tests can
  // assert "URL change reopened the source" etc.
  static instances: FakeEventSource[] = [];
  static calls: ConstructorCall[] = [];

  readonly CONNECTING = FakeEventSource.CONNECTING;
  readonly OPEN = FakeEventSource.OPEN;
  readonly CLOSED = FakeEventSource.CLOSED;

  readyState: number = FakeEventSource.CONNECTING;
  url: string;
  withCredentials: boolean;

  // The hook installs these handlers directly; we drive lifecycle by
  // invoking them in tests.
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null;

  close = vi.fn(() => {
    this.readyState = FakeEventSource.CLOSED;
  });

  constructor(url: string | URL, init?: EventSourceInit) {
    this.url = String(url);
    this.withCredentials = init?.withCredentials ?? false;
    FakeEventSource.calls.push({ url, withCredentials: this.withCredentials });
    FakeEventSource.instances.push(this);
  }

  // --- test helpers ---

  emitOpen(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.call(this as unknown as EventSource, new Event("open"));
  }

  emitMessage(data: string): void {
    // jsdom doesn't construct MessageEvent the same way as the browser, so we
    // hand-roll an object with the only field the hook reads (`data`).
    const ev = { data } as MessageEvent;
    this.onmessage?.call(this as unknown as EventSource, ev);
  }

  emitJsonMessage(payload: RunEvent): void {
    this.emitMessage(JSON.stringify(payload));
  }

  emitErrorClosed(): void {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.call(this as unknown as EventSource, new Event("error"));
  }

  emitErrorTransient(): void {
    this.readyState = FakeEventSource.CONNECTING;
    this.onerror?.call(this as unknown as EventSource, new Event("error"));
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  FakeEventSource.calls = [];
  // Stub at the global level; the hook does `new EventSource(...)` against
  // whatever globalThis.EventSource currently resolves to.
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// --------------------------------------------------------------------------- //
// Test fixtures                                                                //
// --------------------------------------------------------------------------- //

function startedEvent(seq: number): RunEvent {
  return {
    seq,
    type: "run_started",
    ticker: "SPY",
    asset_type: "stock",
    analysis_date: "2026-05-20",
    analysts: ["market"],
    research_depth: 1,
    llm_provider: "openai",
    quick_think_llm: "gpt-4o-mini",
    deep_think_llm: "gpt-4o",
    output_language: "English",
    checkpoint_enabled: false,
    thinking_config: null,
  };
}

function progressEvent(seq: number, progress: number): RunEvent {
  return {
    seq,
    type: "progress_update",
    progress,
    step: `step-${seq}`,
  };
}

// --------------------------------------------------------------------------- //
// Tests                                                                        //
// --------------------------------------------------------------------------- //

describe("useEventSource", () => {
  it("delivers parsed messages in order with monotonic lastSeq", () => {
    const { result } = renderHook(() => useEventSource("/api/runs/abc/events"));

    // After mount, hook should have constructed exactly one source and be
    // in `connecting` state until onopen fires.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(result.current.state).toBe("connecting");
    expect(result.current.events).toEqual([]);
    expect(result.current.lastSeq).toBeNull();

    const src = FakeEventSource.instances[0]!;
    act(() => {
      src.emitOpen();
    });
    expect(result.current.state).toBe("open");

    act(() => {
      src.emitJsonMessage(startedEvent(1));
      src.emitJsonMessage(progressEvent(2, 0.25));
      src.emitJsonMessage(progressEvent(3, 0.5));
    });

    expect(result.current.events).toHaveLength(3);
    expect(result.current.events.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(result.current.lastSeq).toBe(3);
    expect(result.current.error).toBeNull();
  });

  it("resets events and reopens the source on URL change", () => {
    const { result, rerender } = renderHook(
      ({ url }: { url: string }) => useEventSource(url),
      { initialProps: { url: "/api/runs/a/events" } },
    );

    const firstSrc = FakeEventSource.instances[0]!;
    act(() => {
      firstSrc.emitOpen();
      firstSrc.emitJsonMessage(startedEvent(1));
      firstSrc.emitJsonMessage(progressEvent(2, 0.2));
    });
    expect(result.current.events).toHaveLength(2);
    expect(result.current.lastSeq).toBe(2);

    // Swap URLs → hook must close the previous source, open a new one,
    // and clear accumulated state.
    rerender({ url: "/api/runs/b/events" });

    expect(firstSrc.close).toHaveBeenCalledTimes(1);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.calls[1]!.url).toBe("/api/runs/b/events");
    expect(result.current.events).toEqual([]);
    expect(result.current.lastSeq).toBeNull();
    expect(result.current.state).toBe("connecting");
  });

  it("transitions idle → connecting → open → error on hard close", () => {
    const { result } = renderHook(() => useEventSource("/api/runs/x/events"));

    expect(result.current.state).toBe("connecting");

    const src = FakeEventSource.instances[0]!;
    act(() => {
      src.emitOpen();
    });
    expect(result.current.state).toBe("open");

    // Transient error (browser is still trying to reconnect) — hook should
    // step back to "connecting", NOT set an error message.
    act(() => {
      src.emitErrorTransient();
    });
    expect(result.current.state).toBe("connecting");
    expect(result.current.error).toBeNull();

    // Hard close → readyState === CLOSED → hook should surface error.
    act(() => {
      src.emitErrorClosed();
    });
    expect(result.current.state).toBe("error");
    expect(result.current.error).toBe("SSE connection closed");
  });

  it("flags JSON parse errors but keeps processing later valid messages", () => {
    const { result } = renderHook(() => useEventSource("/api/runs/x/events"));
    const src = FakeEventSource.instances[0]!;
    act(() => src.emitOpen());

    act(() => {
      src.emitMessage("not-valid-json{");
    });
    expect(result.current.error).toMatch(/Failed to parse SSE payload/);
    expect(result.current.events).toEqual([]);

    // Subsequent good payload should still land.
    act(() => {
      src.emitJsonMessage(startedEvent(5));
    });
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0]!.seq).toBe(5);
    expect(result.current.lastSeq).toBe(5);
  });

  it("constructs the EventSource with withCredentials=true (preserves Last-Event-ID resume)", () => {
    renderHook(() => useEventSource("/api/runs/x/events"));
    expect(FakeEventSource.calls).toHaveLength(1);
    expect(FakeEventSource.calls[0]!.withCredentials).toBe(true);
    expect(String(FakeEventSource.calls[0]!.url)).toBe("/api/runs/x/events");
  });

  it("closes the source on unmount", () => {
    const { unmount } = renderHook(() => useEventSource("/api/runs/x/events"));
    const src = FakeEventSource.instances[0]!;
    expect(src.close).not.toHaveBeenCalled();
    unmount();
    expect(src.close).toHaveBeenCalledTimes(1);
  });

  it("does not connect when url is null", () => {
    const { result, rerender } = renderHook(
      ({ url }: { url: string | null }) => useEventSource(url),
      { initialProps: { url: null as string | null } },
    );

    // No EventSource constructed; state stays at the idle default.
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(result.current.state).toBe("idle");
    expect(result.current.events).toEqual([]);

    // Once a URL arrives, the hook should connect for the first time.
    rerender({ url: "/api/runs/x/events" });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(result.current.state).toBe("connecting");
  });

  it("does not connect when enabled=false", () => {
    renderHook(() =>
      useEventSource("/api/runs/x/events", { enabled: false }),
    );
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});
