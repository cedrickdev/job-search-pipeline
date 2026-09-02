import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import { renderWithProviders } from "../test/providers";
import { OverviewPage } from "./OverviewPage";

const OV = {
  kpis: {
    phone_screen_readiness: { value: 90, target: 90 },
    response_rate: 0.667,
    velocity: { value: 4, goal: 5 },
    in_flight: 12,
    replies_to_action: 3,
  },
  today: [
    { application_id: 1, job_id: 101, company: "Acme Corp", title: "Senior Sales Assistant",
      status: "Ready to apply", url: "", language: "en", score: 88, phone_screen_pct: 92 },
  ],
  borderline: [
    { application_id: 2, job_id: 102, company: "Globex", title: "Shift Lead",
      status: "Borderline", url: "", language: "en", score: 72, phone_screen_pct: 80 },
  ],
  auto_approved_today: [],
  followups_due: [
    { kind: "applied_no_reply", application_id: 3, job_id: 103, company: "Initech",
      title: "Analyst", days: 5, since: "2026-06-10" },
  ],
  replies_to_action: [
    { application_id: 4, job_id: 104, company: "Umbrella", title: "Lead Sales",
      status: "Recruiter reply", url: "", language: "en", score: 91, phone_screen_pct: 95 },
  ],
  upcoming_interviews: [
    { id: 7, job_id: 105, company: "Soylent", title: "Staff Sales",
      round_label: "Round 1", scheduled_for: "2026-06-20 14:00" },
  ],
  funnel: [
    { stage: "Discovered", count: 40, pct: 1 },
    { stage: "Applied", count: 12, pct: 0.3 },
    { stage: "Interview", count: 2, pct: 0.05 },
  ],
  status_breakdown: [
    { status: "Applied", count: 12 },
    { status: "Recruiter reply", count: 3 },
  ],
  source_health: [{ source: "LinkedIn", discovered: 30, applied: 8 }],
};

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{loc.pathname}</div>;
}

describe("OverviewPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify(OV), { status: 200 }),
    ));
  });

  it("renders KPI tiles and every command-center section", async () => {
    renderWithProviders(<OverviewPage />);
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument());

    // KPI units: readiness=percent, response_rate=fraction→%, velocity={value,goal}
    expect(screen.getByText("Phone-screen readiness")).toBeInTheDocument();
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.getByText("Response rate")).toBeInTheDocument();
    expect(screen.getByText("67%")).toBeInTheDocument();
    expect(screen.getByText("Velocity")).toBeInTheDocument();
    expect(screen.getByText("In flight")).toBeInTheDocument();
    expect(screen.getByText("Replies to action")).toBeInTheDocument();

    // Section headings (spec §220 command-center)
    expect(screen.getByText("Today — do these first")).toBeInTheDocument();
    expect(screen.getByText("Borderline review")).toBeInTheDocument();
    expect(screen.getByText("Auto-approved today")).toBeInTheDocument();
    expect(screen.getByText("Recruiter replies")).toBeInTheDocument();
    expect(screen.getByText("Upcoming interviews")).toBeInTheDocument();
    expect(screen.getByText("Follow-ups due")).toBeInTheDocument();
    expect(screen.getByText("Funnel")).toBeInTheDocument();
    expect(screen.getByText("Pipeline by status")).toBeInTheDocument();
    expect(screen.getByText("Source health")).toBeInTheDocument();

    // Real rows + interview render
    expect(screen.getByText("Soylent")).toBeInTheDocument();

    // Empty section renders cleanly (auto_approved_today is empty)
    expect(screen.getByText("Nothing here right now.")).toBeInTheDocument();
  });

  it("navigates to /jobs when a job row is clicked", async () => {
    renderWithProviders(
      <>
        <OverviewPage />
        <LocationProbe />
      </>,
    );
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Acme Corp"));
    expect(screen.getByTestId("loc").textContent).toBe("/jobs");
  });
});
