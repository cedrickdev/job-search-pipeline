// Copilot chat client (spec §6.1). The backend's POST /api/chat returns an SSE
// stream (sse_starlette EventSourceResponse), so the browser EventSource API
// (GET-only) can't consume it — we POST and read the body as a stream instead.
import { ApiError, apiGet } from "./client";

export type ChatScope = "global" | "job";

export interface ChatEvent {
  // "session" | "token" | "action_proposal" | "done" | "error" | "info"
  event: string;
  data: unknown;
}

export interface StreamChatBody {
  message: string;
  scope: ChatScope;
  scope_id?: number;
}

// One copilot-proposed state change (spec §6.1). The server re-validates type +
// args before applying, so this is only a UI hint — never trusted on its own.
export interface ActionProposal {
  type: string;
  job_id: number;
  args?: Record<string, unknown>;
  label?: string;
}

// The authoritative closing event: `text` is the mandate-sanitized reply that
// MUST replace the streamed token preview; mandate_ok=false means the prose did
// not clear the §6.2 gate and should be shown with a warning, never as ready.
export interface ChatDone {
  text: string;
  session_id: string | null;
  mandate_ok: boolean;
  flags: string[];
}

/**
 * Parse accumulated SSE text into complete events, returning any trailing
 * partial frame as `rest` so the caller can prepend it to the next chunk.
 * Tolerant of `\r\n` separators (sse_starlette's default) and `:` ping/comment
 * lines; each `data:` payload is JSON-parsed, falling back to the raw string.
 */
export function parseSseBuffer(buffer: string): { events: ChatEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: ChatEvent[] = [];
  for (const block of blocks) {
    if (!block.trim()) continue;
    let event = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith(":")) continue; // comment / keepalive ping
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    }
    if (!dataLines.length) continue;
    const raw = dataLines.join("\n");
    let data: unknown = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      /* leave as raw string */
    }
    events.push({ event, data });
  }
  return { events, rest };
}

// One persisted turn replayed by GET /api/chat/history so the panel can rehydrate
// a conversation on mount (spec §6.1) — it no longer vanishes on leaving the chat.
export interface ChatHistoryMessage {
  role: "user" | "assistant";
  text: string;
  created_at: string;
}

/** Fetch a scope's persisted conversation (oldest first); [] when none/unreachable. */
export async function fetchChatHistory(scope: ChatScope, scopeId = 0): Promise<ChatHistoryMessage[]> {
  const res = await apiGet<{ messages?: ChatHistoryMessage[] }>(
    `/api/chat/history?scope=${encodeURIComponent(scope)}&scope_id=${scopeId}`,
  );
  return res?.messages ?? [];
}

/** Stream one copilot turn, invoking `onEvent` for each parsed SSE event. */
export async function streamChat(
  body: StreamChatBody,
  opts: { onEvent: (ev: ChatEvent) => void; signal?: AbortSignal },
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ scope_id: 0, ...body }),
    signal: opts.signal,
  });
  if (!res.ok || !res.body) {
    let detail: unknown = `HTTP ${res.status}`;
    try {
      const j = (await res.json()) as { detail?: unknown };
      if (j && typeof j === "object" && "detail" in j) detail = j.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const { events, rest } = parseSseBuffer(buffer);
    buffer = rest;
    for (const ev of events) opts.onEvent(ev);
  }
  // Flush a final frame that arrived without a trailing blank line.
  const { events } = parseSseBuffer(buffer + "\n\n");
  for (const ev of events) opts.onEvent(ev);
}

/**
 * Map a copilot action proposal to the typed state-change endpoint (spec §5.2).
 * Returns null for types with no endpoint yet (draft_followup → Phase 9), so the
 * UI can render the card but disable Confirm rather than POST to a 404.
 */
export function actionEndpoint(a: ActionProposal): { path: string; body?: unknown } | null {
  const args = a.args ?? {};
  switch (a.type) {
    case "set_status":
      return { path: `/api/jobs/${a.job_id}/status`, body: { status: args.status, detail: args.detail } };
    case "mark_applied":
      return { path: `/api/jobs/${a.job_id}/applied`, body: { channel: args.channel ?? "manual" } };
    case "regen":
      // creativity is the inferred boldness level (conservative|balanced|bold);
      // default to balanced when the copilot omits it. The server re-normalizes.
      return {
        path: `/api/jobs/${a.job_id}/regen`,
        body: { notes: args.notes ?? "", creativity: args.creativity ?? "balanced" },
      };
    case "skip":
      return { path: `/api/jobs/${a.job_id}/skip` };
    default:
      return null;
  }
}
