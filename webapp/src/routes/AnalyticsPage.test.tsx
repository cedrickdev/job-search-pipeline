import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "../test/providers";
import { AnalyticsPage } from "./AnalyticsPage";

const ANALYTICS = {
  days: 30,
  kpis: {
    phone_screen_readiness: { value: 88, target: 90 },
    response_rate: 0.4,
    velocity: { value: 6, goal: 5, window_days: 7 },
  },
  funnel: [
    { stage: "Discovered", count: 40, pct: 1 },
    { stage: "Applied", count: 12, pct: 0.3 },
  ],
  status_breakdown: [
    { status: "Applied", count: 12 },
    { status: "Recruiter reply", count: 3 },
  ],
  applications_per_day: [
    { date: "2026-06-14", count: 0 },
    { date: "2026-06-15", count: 3 },
    { date: "2026-06-16", count: 1 },
  ],
  replies_per_day: [
    { date: "2026-06-14", count: 0 },
    { date: "2026-06-15", count: 2 },
    { date: "2026-06-16", count: 0 },
  ],
  phone_screen_trend: {
    target: 90,
    points: [
      { date: "2026-06-14", value: 85, n: 1 },
      { date: "2026-06-15", value: 92, n: 2 },
    ],
  },
};

describe("AnalyticsPage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify(ANALYTICS), { status: 200 }),
    ));
  });

  it("renders KPI tiles and every trend section", async () => {
    renderWithProviders(<AnalyticsPage />);
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());

    // KPIs reused from the snapshot
    expect(screen.getByText("Phone-screen readiness")).toBeInTheDocument();
    expect(screen.getByText("88%")).toBeInTheDocument();
    expect(screen.getByText("Response rate")).toBeInTheDocument();
    expect(screen.getByText("40%")).toBeInTheDocument();
    expect(screen.getByText("Velocity")).toBeInTheDocument();

    // Trend section headings
    expect(screen.getByText("Applications per day")).toBeInTheDocument();
    expect(screen.getByText("Recruiter replies per day")).toBeInTheDocument();
    expect(screen.getByText("Phone-screen readiness trend")).toBeInTheDocument();
    expect(screen.getByText("Funnel")).toBeInTheDocument();
    expect(screen.getByText("Pipeline by status")).toBeInTheDocument();
  });

  it("renders an empty-state for a trend with no data points", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(
        JSON.stringify({ ...ANALYTICS, phone_screen_trend: { target: 90, points: [] } }),
        { status: 200 },
      ),
    ));
    renderWithProviders(<AnalyticsPage />);
    await waitFor(() => expect(screen.getByText("Analytics")).toBeInTheDocument());
    expect(screen.getByText("No scored CVs in this window.")).toBeInTheDocument();
  });

  it("shows an error state when the request fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 500 })));
    renderWithProviders(<AnalyticsPage />);
    await waitFor(() =>
      expect(screen.getByText("Could not load analytics.")).toBeInTheDocument(),
    );
  });
});
