import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../test/providers";
import { CopilotPanel } from "./CopilotPanel";
import { streamChat, type ChatEvent } from "../api/chat";

// Mock only streamChat; keep the real actionEndpoint mapping so Confirm exercises
// the genuine §5.2 endpoint resolution.
vi.mock("../api/chat", async (orig) => {
  const actual = await orig<typeof import("../api/chat")>();
  return { ...actual, streamChat: vi.fn() };
});

const streamMock = streamChat as unknown as Mock;

// Drive the panel with a canned event sequence on the next turn.
function nextTurn(...events: ChatEvent[]) {
  streamMock.mockImplementationOnce(
    async (_body: unknown, opts: { onEvent: (e: ChatEvent) => void }) => {
      for (const e of events) opts.onEvent(e);
    },
  );
}

async function ask(text: string) {
  await userEvent.type(screen.getByRole("textbox", { name: /message copilot/i }), text);
  await userEvent.click(screen.getByRole("button", { name: /^ask$/i }));
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ status: "applied" }), { status: 200 })));
});
afterEach(() => vi.restoreAllMocks());

describe("CopilotPanel", () => {
  it("shows the user message and the authoritative done text", async () => {
    nextTurn(
      { event: "token", data: "Strea" },
      { event: "token", data: "ming…" },
      { event: "done", data: { text: "Here is my advice.", session_id: "s1", mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="global" />);
    await ask("what next?");
    expect(await screen.findByText("what next?")).toBeInTheDocument();
    expect(await screen.findByText("Here is my advice.")).toBeInTheDocument();
  });

  it("renders assistant Markdown — bold and lists, with no literal markup left", async () => {
    nextTurn({
      event: "done",
      data: { text: "Try **this** first:\n\n- one\n- two", session_id: null, mandate_ok: true, flags: [] },
    });
    const { container } = renderWithProviders(<CopilotPanel scope="global" />);
    await ask("advice?");

    // Bold renders as <strong>, not literal asterisks.
    await waitFor(() =>
      expect(container.querySelector(".copilot-msg.assistant strong")).toHaveTextContent("this"),
    );
    // The list renders as two <li> items.
    expect(container.querySelectorAll(".copilot-msg.assistant li")).toHaveLength(2);
    // No raw Markdown markers survive in the rendered text.
    expect(container.textContent).not.toContain("**this**");
  });

  it("renders an action card and POSTs to the typed endpoint on Confirm", async () => {
    nextTurn(
      { event: "action_proposal", data: { type: "set_status", job_id: 7, args: { status: "applied" }, label: "Mark as applied" } },
      { event: "done", data: { text: "Done.", session_id: null, mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="job" scopeId={7} />);
    await ask("mark it applied");

    await screen.findByText("Mark as applied");
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    const fetchMock = global.fetch as Mock;
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/api/jobs/7/status"));
      expect(call).toBeTruthy();
      expect(JSON.parse(call![1].body)).toEqual({ status: "applied" });
    });
    // Card is consumed once confirmed.
    await waitFor(() => expect(screen.queryByText("Mark as applied")).not.toBeInTheDocument());
  });

  it("leaves a queued-feedback line in the log after confirming a regen", async () => {
    nextTurn(
      { event: "action_proposal", data: { type: "regen", job_id: 7, args: { notes: "More NLP" }, label: "Regenerate CV" } },
      { event: "done", data: { text: "On it.", session_id: null, mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="job" scopeId={7} />);
    await ask("regen the cv");

    await screen.findByText("Regenerate CV");
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    // The card is consumed and a confirmation line lands in the log at the trigger
    // point so the user knows the regen was queued (it renders on the next run).
    await waitFor(() => expect(screen.queryByText("Regenerate CV")).not.toBeInTheDocument());
    expect(await screen.findByText(/queued/i)).toBeInTheDocument();
  });

  it("echoes the inferred creativity in the regen confirmation line", async () => {
    nextTurn(
      { event: "action_proposal", data: { type: "regen", job_id: 7,
        args: { notes: "max JD match", creativity: "bold" }, label: "Regenerate CV" } },
      { event: "done", data: { text: "On it.", session_id: null, mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="job" scopeId={7} />);
    // Prompt deliberately omits the word "bold" so the assertion can only match
    // the confirmation line, never the echoed user message.
    await ask("tailor this cv aggressively for the JD");

    await screen.findByText("Regenerate CV");
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    // Restating the boldness level confirms the copilot understood "be bold".
    expect(await screen.findByText(/\(bold\)/i)).toBeInTheDocument();
  });

  it("dismisses an action card without POSTing", async () => {
    nextTurn(
      { event: "action_proposal", data: { type: "skip", job_id: 9, label: "Skip this role" } },
      { event: "done", data: { text: "ok", session_id: null, mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="job" scopeId={9} />);
    await ask("skip");

    await screen.findByText("Skip this role");
    await userEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(screen.queryByText("Skip this role")).not.toBeInTheDocument();

    const fetchMock = global.fetch as Mock;
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("/skip"))).toBe(false);
  });

  it("lets the user edit an action's args before confirming", async () => {
    nextTurn(
      { event: "action_proposal", data: { type: "set_status", job_id: 7, args: { status: "applied" }, label: "Mark as applied" } },
      { event: "done", data: { text: "Done.", session_id: null, mandate_ok: true, flags: [] } },
    );
    renderWithProviders(<CopilotPanel scope="job" scopeId={7} />);
    await ask("change status");

    await screen.findByText("Mark as applied");
    await userEvent.click(screen.getByRole("button", { name: /edit/i }));
    const ta = screen.getByRole("textbox", { name: /edit action/i });
    fireEvent.change(ta, { target: { value: '{"status":"interview"}' } });
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    const fetchMock = global.fetch as Mock;
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/api/jobs/7/status"));
      expect(call).toBeTruthy();
      expect(JSON.parse(call![1].body)).toEqual({ status: "interview" });
    });
  });

  it("hydrates the persisted conversation on mount", async () => {
    // The user's complaint: leaving the chat lost the thread. On mount the panel
    // replays GET /api/chat/history for its scope and renders the stored turns.
    (global.fetch as Mock).mockImplementation(async (url: string) => {
      if (String(url).includes("/api/chat/history")) {
        return new Response(
          JSON.stringify({
            messages: [
              { role: "user", text: "earlier question", created_at: "2026-06-16T09:00:00" },
              { role: "assistant", text: "earlier answer", created_at: "2026-06-16T09:00:01" },
            ],
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ status: "applied" }), { status: 200 });
    });
    renderWithProviders(<CopilotPanel scope="job" scopeId={42} />);
    expect(await screen.findByText("earlier question")).toBeInTheDocument();
    expect(await screen.findByText("earlier answer")).toBeInTheDocument();
  });

  it("warns when the reply fails the mandate gate", async () => {
    nextTurn({
      event: "done",
      data: { text: "draft", session_id: null, mandate_ok: false, flags: ["anonymization_config_missing"] },
    });
    renderWithProviders(<CopilotPanel scope="global" />);
    await ask("draft something");
    expect(await screen.findByText(/anonymization_config_missing/)).toBeInTheDocument();
  });
});
