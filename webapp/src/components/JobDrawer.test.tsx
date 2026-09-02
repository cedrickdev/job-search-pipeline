import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../test/providers";
import { JobDrawer } from "./JobDrawer";

const DETAIL = {
  job: { id: 10, company: "Alpha", title: "Shift Lead", url: "https://x", location: "Lausanne",
         language: "en", description: "Retail and service." },
  application: { id: 1, status: "Ready to apply", submitted_at: null, recruiter_email: null },
  score: { score: 91, reasoning: "Strong match" },
  fit: { available: true, coverage_score: 0.82, risk_tier: "GREEN",
         matched_keywords: ["python"], missing_keywords: ["spark"] },
  cv_versions: { en: { id: 5, language: "en", phone_screen_pct: 88,
                       pdf_url: "/api/files/cv/5", created_at: "2026-06-11T07:00:00" }, fr: null },
  cover_letter: { en: null, fr: null },
  events: [
    { id: 2, event_type: "status_change", detail: "Ready to apply", source: "manual",
      created_at: "2026-06-12T10:00:00" },
    { id: 1, event_type: "discovered", detail: "found on wtj", source: "pipeline",
      created_at: "2026-06-10T09:00:00" },
  ],
  pending_regen: null,
  last_regen: null,
  last_apply: null,
};

const IDLE: unknown = { state: "idle", kind: null, started_at: null, last_run: null };
const FULL_RUNNING: unknown = {
  state: "running", kind: "full", started_at: "2026-06-16T08:30:00", last_run: null,
};

// Same job with a CV regeneration queued (awaiting the next agentic run). While
// queued, last_regen mirrors the pending request.
const PENDING_DETAIL = {
  ...DETAIL,
  pending_regen: {
    request_id: 42, notes: "Lead on NLP and LLMs", creativity: "bold",
    created_at: "2026-06-16T08:00:00",
  },
  last_regen: {
    request_id: 42, status: "pending", notes: "Lead on NLP and LLMs",
    creativity: "bold", created_at: "2026-06-16T08:00:00", resolved_at: null,
    detail: null,
  },
};

// A regeneration that failed after retries — no longer pending, reason recorded.
const FAILED_DETAIL = {
  ...DETAIL,
  pending_regen: null,
  last_regen: {
    request_id: 42, status: "failed", notes: "Lead on NLP and LLMs",
    creativity: "bold", created_at: "2026-06-16T08:00:00",
    resolved_at: "2026-06-16T09:00:00", detail: "tailoring failed after 3 attempts",
  },
};

// A regeneration the background run completed and applied.
const DONE_DETAIL = {
  ...DETAIL,
  pending_regen: null,
  last_regen: {
    request_id: 42, status: "done", notes: "More econometrics",
    creativity: "balanced", created_at: "2026-06-16T08:00:00",
    resolved_at: "2026-06-16T09:00:00", detail: null,
  },
};

// URL-aware stub: job detail, run status (controls the running/idle chip), and a
// 202 for any POST (e.g. the Run-now trigger). Returns the fetch mock so tests
// can assert the Run-now POST hit /api/runs/full.
function stubApi(detail: unknown, runStatus: unknown = IDLE) {
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (init?.method === "POST")
      return new Response(JSON.stringify({ ok: true }), { status: 202 });
    if (u.includes("/api/runs/status"))
      return new Response(JSON.stringify(runStatus), { status: 200 });
    return new Response(JSON.stringify(detail), { status: 200 });
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("JobDrawer", () => {
  beforeEach(() => {
    stubApi(DETAIL);
  });

  it("renders job detail", async () => {
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.getByText(/Shift Lead/)).toBeInTheDocument();
    expect(screen.getByText("91")).toBeInTheDocument();
  });

  it("shows the activity timeline on the Activity tab", async () => {
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^activity$/i }));
    expect(screen.getByText(/discovered/)).toBeInTheDocument();
    expect(screen.getByText(/found on wtj/)).toBeInTheDocument();
  });

  it("shows a job-scoped copilot on the Copilot tab", async () => {
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^copilot$/i }));
    expect(screen.getByRole("region", { name: /copilot/i })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /message copilot/i })).toBeInTheDocument();
  });

  it("requires confirm before posting an action", async () => {
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /approve/i }));
    // No POST yet — confirm bar is showing
    expect(fetchMock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(0);
    expect(screen.getByText(/confirm/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(
        (c) => typeof c[0] === "string" && c[0].includes("/go") && c[1]?.method === "POST",
      )).toBe(true),
    );
  });

  it("shows a queued CV-regeneration chip with its creativity when no run is active", async () => {
    stubApi(PENDING_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    // Queued, not running: honest "queued" wording + the inferred boldness level.
    expect(screen.getByText(/CV regen queued \(bold\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/regenerating now/i)).not.toBeInTheDocument();
  });

  it("shows a 'regenerating now' chip while a full run is active", async () => {
    stubApi(PENDING_DETAIL, FULL_RUNNING);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    // A live run is consuming the queue, so the chip reflects real progress.
    await waitFor(() =>
      expect(screen.getByText(/regenerating now/i)).toBeInTheDocument());
    expect(screen.queryByText(/regen queued/i)).not.toBeInTheDocument();
  });

  it("shows a failed chip when the last regeneration failed", async () => {
    stubApi(FAILED_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.getByText(/CV regen failed/i)).toBeInTheDocument();
  });

  it("explains the queued CV regeneration with its notes and creativity on the CV tab", async () => {
    stubApi(PENDING_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^cv$/i }));
    expect(screen.getByText(/Lead on NLP and LLMs/)).toBeInTheDocument();
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(/regeneration queued/i);
    expect(banner).toHaveTextContent(/bold/i);
  });

  it("processes a queued regen via the Run-now button (POSTs /api/runs/full)", async () => {
    const mock = stubApi(PENDING_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^cv$/i }));
    await userEvent.click(screen.getByRole("button", { name: /run pipeline now/i }));
    await waitFor(() =>
      expect(mock.mock.calls.some(
        (c) => typeof c[0] === "string" && c[0].includes("/api/runs/full") &&
          c[1]?.method === "POST",
      )).toBe(true),
    );
  });

  it("disables the Run-now button while a full run is active", async () => {
    stubApi(PENDING_DETAIL, FULL_RUNNING);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^cv$/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /run pipeline now/i })).toBeDisabled());
  });

  it("shows the failure reason and a retry on the CV tab when a regen failed", async () => {
    stubApi(FAILED_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^cv$/i }));
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(/regeneration failed/i);
    expect(banner).toHaveTextContent(/tailoring failed after 3 attempts/i);
    // Failed → offer a retry via the same full-run trigger.
    expect(screen.getByRole("button", { name: /run pipeline now/i })).toBeInTheDocument();
  });

  it("confirms a completed regeneration on the CV tab", async () => {
    stubApi(DONE_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^cv$/i }));
    expect(screen.getByText(/regeneration applied/i)).toBeInTheDocument();
  });

  it("shows no regen indicator when nothing is pending or recorded", async () => {
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.queryByText(/CV regen/i)).not.toBeInTheDocument();
  });

  const APPLYING_DETAIL = {
    ...DETAIL,
    last_apply: {
      request_id: 7, status: "in_progress", channel: "linkedin",
      detail: null, screenshot_path: null,
      created_at: "2026-06-18T08:00:00", resolved_at: null,
    },
  };
  const APPLIED_DETAIL = {
    ...DETAIL,
    application: { ...DETAIL.application, status: "Applied" },
    last_apply: {
      request_id: 7, status: "applied", channel: "linkedin",
      detail: "Submitted via Easy Apply", screenshot_path: "data/screenshots/10_done.png",
      created_at: "2026-06-18T08:00:00", resolved_at: "2026-06-18T08:03:00",
    },
  };
  const NEEDS_YOU_APPLY_DETAIL = {
    ...DETAIL,
    last_apply: {
      request_id: 7, status: "needs_you", channel: "linkedin",
      detail: "Browser in use. Close the live session and retry.",
      screenshot_path: null,
      created_at: "2026-06-18T08:00:00", resolved_at: "2026-06-18T08:00:05",
    },
  };
  const FAILED_APPLY_DETAIL = {
    ...DETAIL,
    last_apply: {
      request_id: 7, status: "failed", channel: "wtj",
      detail: "Unhandled exception: boom", screenshot_path: null,
      created_at: "2026-06-18T08:00:00", resolved_at: "2026-06-18T08:00:09",
    },
  };

  it("shows an Apply-now button for a ready job with a CV", async () => {
    stubApi(DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^apply now$/i })).toBeInTheDocument();
  });

  it("fires Apply-now for a ready job with a CV", async () => {
    const mock = stubApi(DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /^apply now$/i }));
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(
        mock.mock.calls.some(
          (c) =>
            typeof c[0] === "string" &&
            c[0].endsWith("/api/jobs/10/apply-now") &&
            c[1]?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("shows Applying-now and hides the Apply-now button while mid-apply", async () => {
    stubApi(APPLYING_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/applying now/i)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^apply now$/i })).toBeNull();
  });

  it("shows an Applied banner", async () => {
    stubApi(APPLIED_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("✓ Applied")).toBeInTheDocument());
    expect(screen.getByText(/Submitted via Easy Apply/)).toBeInTheDocument();
  });

  it("shows a Needs-you banner and retries via Apply-now", async () => {
    const mock = stubApi(NEEDS_YOU_APPLY_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("⚠ Needs you")).toBeInTheDocument());
    expect(screen.getByText(/Browser in use/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(
        mock.mock.calls.some(
          (c) =>
            typeof c[0] === "string" &&
            c[0].endsWith("/api/jobs/10/apply-now") &&
            c[1]?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("shows a Failed banner with the reason", async () => {
    stubApi(FAILED_APPLY_DETAIL, IDLE);
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/failed/i)).toBeInTheDocument());
    expect(screen.getByText(/Unhandled exception: boom/)).toBeInTheDocument();
  });

  it("changes the status to any value through the confirm-gated selector", async () => {
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    renderWithProviders(<JobDrawer jobId={10} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());

    // Pick an arbitrary lifecycle status the fixed buttons cannot reach.
    const select = screen.getByRole("combobox", { name: /change status/i });
    fireEvent.change(select, { target: { value: "Interview scheduled" } });

    // Confirm-gated: nothing is posted until the user confirms.
    expect(fetchMock.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(0);
    expect(screen.getByText(/Change status to Interview scheduled/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          (c) =>
            typeof c[0] === "string" &&
            c[0].endsWith("/api/jobs/10/status") &&
            c[1]?.method === "POST" &&
            JSON.parse(c[1]!.body as string).status === "Interview scheduled",
        ),
      ).toBe(true),
    );
  });
});
