import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "../test/providers";
import { FollowupsPanel } from "./FollowupsPanel";
import type { FollowupItem } from "../api/types";

const ITEMS: FollowupItem[] = [
  { kind: "applied_no_reply", application_id: 3, job_id: 103, company: "Initech",
    title: "Analyst", days: 9, since: "2026-06-07" },
  { kind: "recruiter_no_outbound", application_id: 4, job_id: 104, company: "Globex",
    title: "Shift Lead", days: 3, since: "2026-06-13" },
];

const DRAFT = {
  subject: "Follow-up: Analyst application at Initech",
  body: "Dear Hiring Team,\n\nbackground in retail, customer service and weekend shifts.",
  language: "en",
  mandate_ok: true,
  flags: [],
};

describe("FollowupsPanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify(DRAFT), { status: 200 }),
    ));
  });

  it("lists each due follow-up with its reason", () => {
    renderWithProviders(<FollowupsPanel items={ITEMS} onOpen={() => {}} />);
    expect(screen.getByText("Follow-ups due")).toBeInTheDocument();
    expect(screen.getByText("Initech")).toBeInTheDocument();
    expect(screen.getByText("Globex")).toBeInTheDocument();
    expect(screen.getByText(/no reply/)).toBeInTheDocument();
    expect(screen.getByText(/no outbound/)).toBeInTheDocument();
  });

  it("renders a friendly empty state", () => {
    renderWithProviders(<FollowupsPanel items={[]} onOpen={() => {}} />);
    expect(screen.getByText("No follow-ups due.")).toBeInTheDocument();
  });

  it("drafts a follow-up and shows the subject and body", async () => {
    renderWithProviders(<FollowupsPanel items={[ITEMS[0]]} onOpen={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /draft/i }));
    await waitFor(() =>
      expect(screen.getByText(/weekend shifts/)).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Follow-up: Analyst application at Initech"),
    ).toBeInTheDocument();
  });

  it("warns when a draft does not clear the mandate gate", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(
        JSON.stringify({ ...DRAFT, mandate_ok: false, flags: ["forbidden_client:X"] }),
        { status: 200 },
      ),
    ));
    renderWithProviders(<FollowupsPanel items={[ITEMS[0]]} onOpen={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /draft/i }));
    await waitFor(() =>
      expect(screen.getByText(/review before sending/i)).toBeInTheDocument(),
    );
  });

  it("dismisses a follow-up via the dismiss endpoint", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }));
    renderWithProviders(<FollowupsPanel items={[ITEMS[0]]} onOpen={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/jobs/103/followup/dismiss"))).toBe(true),
    );
  });
});
