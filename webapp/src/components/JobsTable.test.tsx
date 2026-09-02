import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders } from "../test/providers";
import { JobsTable } from "./JobsTable";

const ROWS = {
  items: [
    { application_id: 1, job_id: 10, company: "Alpha", title: "Shift Lead",
      status: "Ready to apply", url: "u", language: "en", score: 91, phone_screen_pct: 88 },
    { application_id: 2, job_id: 11, company: "Beta", title: "Sales",
      status: "Applied", url: "u", language: "en", score: 70, phone_screen_pct: null },
  ],
};

describe("JobsTable", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify(ROWS), { status: 200 }),
    ));
  });

  it("renders rows from the API", async () => {
    renderWithProviders(<JobsTable onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.getByText("91")).toBeInTheDocument();
  });

  it("calls onOpen with job_id on row click", async () => {
    const onOpen = vi.fn();
    const user = (await import("@testing-library/user-event")).default;
    renderWithProviders(<JobsTable onOpen={onOpen} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    await user.click(screen.getByText("Alpha"));
    expect(onOpen).toHaveBeenCalledWith(10);
  });
});
