import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../test/providers";
import { PrepTab } from "./PrepTab";

const PREP = {
  notes_md: "existing notes",
  likely_questions: ["Tell me about a hard project"],
  company_research: null,
  talking_points: null,
  generated_at: "2026-06-10T10:00:00",
  interviews: [{ id: 1, round_label: "Phone", scheduled_for: "2026-06-20T10:00:00", outcome: null, notes: null, created_at: "2026-06-11T09:00:00" }],
};

describe("PrepTab", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method && init.method !== "GET")
        return new Response(JSON.stringify({ ok: true, id: 2 }), { status: 200 });
      return new Response(JSON.stringify(PREP), { status: 200 });
    }));
  });

  it("shows notes, likely questions, and interview rows", async () => {
    renderWithProviders(<PrepTab jobId={7} />);
    await waitFor(() => expect(screen.getByDisplayValue("existing notes")).toBeInTheDocument());
    expect(screen.getByText(/Tell me about a hard project/)).toBeInTheDocument();
    expect(screen.getByText(/Phone/)).toBeInTheDocument();
  });

  it("saves notes (PUT) on blur", async () => {
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    renderWithProviders(<PrepTab jobId={7} />);
    const ta = await screen.findByDisplayValue("existing notes");
    await userEvent.clear(ta);
    await userEvent.type(ta, "new note");
    await userEvent.tab(); // blur
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(
        (c) => typeof c[0] === "string" && c[0].includes("/prep/notes") && c[1]?.method === "PUT",
      )).toBe(true),
    );
  });
});

const EMPTY_PREP = {
  notes_md: "", likely_questions: null, company_research: null,
  talking_points: null, generated_at: null, interviews: [],
};

describe("PrepTab generate", () => {
  function stub(generateBody: object) {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (typeof url === "string" && url.includes("/prep/generate") && init?.method === "POST")
        return new Response(JSON.stringify(generateBody), { status: 200 });
      return new Response(JSON.stringify(EMPTY_PREP), { status: 200 });
    }));
  }

  it("generates prep and shows the new questions", async () => {
    stub({ ...EMPTY_PREP, likely_questions: ["Why this team?"], generated_at: "2026-06-16T12:00:00", mandate_ok: true, flags: [] });
    renderWithProviders(<PrepTab jobId={7} />);
    await screen.findByRole("button", { name: /generate prep/i });
    await userEvent.click(screen.getByRole("button", { name: /generate prep/i }));
    expect(await screen.findByText("Why this team?")).toBeInTheDocument();
  });

  it("warns when generated prep fails the mandate gate", async () => {
    stub({ ...EMPTY_PREP, likely_questions: ["draft q"], generated_at: null, mandate_ok: false, flags: ["anonymization_config_missing"] });
    renderWithProviders(<PrepTab jobId={7} />);
    await userEvent.click(await screen.findByRole("button", { name: /generate prep/i }));
    expect(await screen.findByText(/anonymization_config_missing/)).toBeInTheDocument();
  });
});
