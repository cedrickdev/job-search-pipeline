import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AppShell } from "./AppShell";
import { renderWithProviders } from "../test/providers";

function renderShell() {
  return render(
    <MemoryRouter>
      <AppShell>
        <div>content</div>
      </AppShell>
    </MemoryRouter>,
  );
}

describe("AppShell", () => {
  it("renders nav links and content", () => {
    renderShell();
    expect(screen.getByRole("link", { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /jobs/i })).toBeInTheDocument();
    expect(screen.getByText("content")).toBeInTheDocument();
  });

  it("theme toggle flips data-theme", async () => {
    renderShell();
    const before = document.documentElement.getAttribute("data-theme");
    await userEvent.click(screen.getByRole("button", { name: /toggle theme/i }));
    expect(document.documentElement.getAttribute("data-theme")).not.toBe(before);
  });

  it("toggles the global copilot panel", async () => {
    renderWithProviders(<AppShell><div>content</div></AppShell>);
    expect(screen.queryByRole("region", { name: /copilot/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /copilot/i }));
    expect(screen.getByRole("region", { name: /copilot/i })).toBeInTheDocument();
  });
});
