import { describe, it, expect, vi, beforeEach, type Mock } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "../test/providers";
import { Board } from "./Board";

const BOARD = {
  board: {
    "Ready to apply": [
      { application_id: 1, job_id: 10, company: "Alpha", title: "ML",
        status: "Ready to apply", url: "u", language: "en", score: 91, phone_screen_pct: 88 },
    ],
    Applied: [
      { application_id: 2, job_id: 11, company: "Beta", title: "Sales",
        status: "Applied", url: "u", language: "en", score: 70, phone_screen_pct: null },
    ],
  },
};

describe("Board", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return new Response(JSON.stringify({ status: {} }), { status: 200 });
        }
        return new Response(JSON.stringify(BOARD), { status: 200 });
      }),
    );
  });

  it("renders columns with cards", async () => {
    renderWithProviders(<Board onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    expect(screen.getByText("Beta")).toBeInTheDocument();
    // column headers present
    expect(screen.getByRole("heading", { name: /Ready to apply/ })).toBeInTheDocument();
  });

  it("changes status by dragging a card onto another column", async () => {
    const { container } = renderWithProviders(<Board onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());

    const card = screen.getByText("Alpha").closest(".board-card")!;
    const appliedCol = container.querySelector('[data-status="Applied"]')!;

    fireEvent.dragStart(card);
    fireEvent.dragOver(appliedCol);
    fireEvent.drop(appliedCol);

    await waitFor(() => {
      const f = global.fetch as Mock;
      const call = f.mock.calls.find(
        (c) => String(c[0]).endsWith("/api/jobs/10/status") && c[1]?.method === "POST",
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(call![1].body as string)).toEqual({ status: "Applied" });
    });
  });

  it("does not POST when a card is dropped on its own column", async () => {
    const { container } = renderWithProviders(<Board onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());

    const card = screen.getByText("Alpha").closest(".board-card")!;
    const sameCol = container.querySelector('[data-status="Ready to apply"]')!;

    fireEvent.dragStart(card);
    fireEvent.drop(sameCol);

    const f = global.fetch as Mock;
    expect(f.mock.calls.some((c) => c[1]?.method === "POST")).toBe(false);
  });

  it("renders every lifecycle status as a drop target, even empty ones", async () => {
    const { container } = renderWithProviders(<Board onOpen={() => {}} />);
    await waitFor(() => expect(screen.getByText("Alpha")).toBeInTheDocument());
    // "Rejected" has no cards in the fixture but must still be a droppable column.
    expect(container.querySelector('[data-status="Rejected"]')).toBeTruthy();
  });
});
