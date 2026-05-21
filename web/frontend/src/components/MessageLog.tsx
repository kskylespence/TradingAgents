import { useEffect, useRef, useState } from "react";

import type { MessageEvent as RunMessageEvent, MessageKind } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Per-kind colour mapping. Kept subtle — the log is read at glance, the
 * colour is just a navigational cue.
 */
const KIND_STYLES: Record<MessageKind, string> = {
  User: "text-blue-600 dark:text-blue-400",
  Agent: "text-foreground",
  Data: "text-emerald-600 dark:text-emerald-400",
  Control: "text-amber-600 dark:text-amber-400",
  System: "text-muted-foreground",
};

const KIND_LABEL: Record<MessageKind, string> = {
  User: "USR",
  Agent: "AGT",
  Data: "DAT",
  Control: "CTL",
  System: "SYS",
};

/** Pixel threshold from the bottom that still counts as "at the bottom". */
const STICK_THRESHOLD_PX = 32;

export interface MessageLogProps {
  messages: RunMessageEvent[];
  className?: string;
}

/**
 * Scrollable, append-only log with sticky-to-bottom semantics:
 *  - We auto-scroll only when the user is already at (or very near) the
 *    bottom. If they've scrolled up to read history, new messages do not
 *    yank them back.
 *  - Detection uses scrollHeight - (scrollTop + clientHeight) — the
 *    pseudo-standard "distance from bottom" calc.
 */
export function MessageLog({ messages, className }: MessageLogProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  // Track whether the user is pinned to the bottom.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - (el.scrollTop + el.clientHeight);
      setStickToBottom(distance <= STICK_THRESHOLD_PX);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // Auto-scroll on new messages (only when pinned).
  useEffect(() => {
    if (!stickToBottom) return;
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, stickToBottom]);

  return (
    <div
      ref={ref}
      className={cn(
        "h-[28rem] overflow-y-auto rounded-md border bg-card font-mono text-xs",
        className,
      )}
      aria-label="Message log"
      role="log"
    >
      {messages.length === 0 ? (
        <div className="p-4 text-muted-foreground">No messages yet.</div>
      ) : (
        <ul className="flex flex-col">
          {messages.map((m, idx) => (
            <li
              key={`${m.seq}-${idx}`}
              className={cn(
                "flex gap-3 border-b px-3 py-1.5 last:border-b-0",
                KIND_STYLES[m.kind],
              )}
            >
              <span className="shrink-0 select-none text-muted-foreground">
                {formatTime(m.timestamp)}
              </span>
              <span className="shrink-0 select-none font-semibold">
                {KIND_LABEL[m.kind]}
              </span>
              <span className="whitespace-pre-wrap break-words">{m.content}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function formatTime(ts: string): string {
  // Accept ISO strings; render HH:MM:SS in the user's locale. Fall back to
  // raw string if parsing fails (defensive — the server emits ISO-8601).
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString(undefined, { hour12: false });
}

export default MessageLog;
