import { describe, it, expect, vi, afterEach } from "vitest";
import { parseSseBuffer, streamChat, actionEndpoint } from "./chat";
import { ApiError } from "./client";
import type { ChatEvent } from "./chat";

afterEach(() => vi.restoreAllMocks());

// Build a streaming Response whose body emits the given raw SSE chunks, the way
// the POST /api/chat EventSourceResponse does on the wire.
function streamResponse(chunks: string[], status = 200) {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch));
      c.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

describe("parseSseBuffer", () => {
  it("splits frames, JSON-parses data, and keeps the trailing partial as rest", () => {
    const { events, rest } = parseSseBuffer(
      'event: token\ndata: "Hel"\n\nevent: token\ndata: "lo"\n\nevent: do',
    );
    expect(events).toEqual([
      { event: "token", data: "Hel" },
      { event: "token", data: "lo" },
    ]);
    expect(rest).toBe("event: do");
  });

  it("handles \\r\\n separators and skips comment/ping lines", () => {
    const { events } = parseSseBuffer(
      ": ping - keepalive\r\n\r\nevent: done\r\ndata: {\"text\":\"hi\",\"mandate_ok\":true}\r\n\r\n",
    );
    expect(events).toEqual([{ event: "done", data: { text: "hi", mandate_ok: true } }]);
  });
});

describe("streamChat", () => {
  it("POSTs the scope/message and dispatches parsed events across chunk boundaries", async () => {
    const fetchMock = vi.fn(async () =>
      streamResponse([
        'event: token\r\ndata: "He',
        'llo"\r\n\r\nevent: done\r\ndata: {"text":"Hello","session_id":null,"mandate_ok":true,"flags":[]}\r\n\r\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const seen: ChatEvent[] = [];
    await streamChat({ message: "hi", scope: "job", scope_id: 7 }, { onEvent: (e) => seen.push(e) });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/chat");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init!.body as string)).toEqual({ message: "hi", scope: "job", scope_id: 7 });
    // The token frame is split mid-data across the two chunks; it must reassemble
    // into one "Hello" token, proving the buffer survives chunk boundaries.
    expect(seen).toEqual([
      { event: "token", data: "Hello" },
      { event: "done", data: { text: "Hello", session_id: null, mandate_ok: true, flags: [] } },
    ]);
  });

  it("throws ApiError when the response is not ok", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail: "job not found" }), { status: 404 })));
    await expect(
      streamChat({ message: "x", scope: "job", scope_id: 1 }, { onEvent: () => {} }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("actionEndpoint", () => {
  it("maps each wired action type to its typed endpoint + body", () => {
    expect(actionEndpoint({ type: "set_status", job_id: 7, args: { status: "applied" } }))
      .toEqual({ path: "/api/jobs/7/status", body: { status: "applied", detail: undefined } });
    expect(actionEndpoint({ type: "mark_applied", job_id: 7, args: { channel: "linkedin" } }))
      .toEqual({ path: "/api/jobs/7/applied", body: { channel: "linkedin" } });
    expect(actionEndpoint({ type: "mark_applied", job_id: 7 }))
      .toEqual({ path: "/api/jobs/7/applied", body: { channel: "manual" } });
    // regen defaults creativity to balanced when the copilot omits it…
    expect(actionEndpoint({ type: "regen", job_id: 7, args: { notes: "more metrics" } }))
      .toEqual({ path: "/api/jobs/7/regen", body: { notes: "more metrics", creativity: "balanced" } });
    // …and passes an inferred boldness level straight through.
    expect(actionEndpoint({ type: "regen", job_id: 7, args: { notes: "max JD match", creativity: "bold" } }))
      .toEqual({ path: "/api/jobs/7/regen", body: { notes: "max JD match", creativity: "bold" } });
    expect(actionEndpoint({ type: "skip", job_id: 7 })).toEqual({ path: "/api/jobs/7/skip" });
  });

  it("returns null for draft_followup (no endpoint until Phase 9)", () => {
    expect(actionEndpoint({ type: "draft_followup", job_id: 7, args: { tone: "warm" } })).toBeNull();
  });
});
