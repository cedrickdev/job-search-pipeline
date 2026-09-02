import { describe, it, expect, vi, beforeEach, type Mock } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "../test/providers";
import { SettingsPage } from "./SettingsPage";

const SETTINGS = {
  auto_apply: false,
  auto_apply_min_score: 85,
  auto_apply_daily_cap: 5,
  tailor_creativity: "balanced",
  llm_backend: "claude_cli",
  llm_base_url: "",
  llm_model: "",
  schedule_enabled: true,
  schedule_time: "08:00",
  schedule_cadence: "daily",
};

describe("SettingsPage", () => {
  let puts: Record<string, unknown>[];

  beforeEach(() => {
    puts = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const u = String(url);
        if (init?.method === "PUT") {
          const body = JSON.parse(init.body as string);
          puts.push(body);
          return new Response(JSON.stringify({ settings: body, status: "ok" }), { status: 200 });
        }
        if (u.includes("/api/runs/status")) {
          return new Response(
            JSON.stringify({ state: "idle", kind: null, started_at: null, last_run: null }),
            { status: 200 },
          );
        }
        if (u.includes("/api/runs/")) {
          return new Response(null, { status: 202 });
        }
        return new Response(JSON.stringify({ settings: SETTINGS, status: "missing" }), { status: 200 });
      }),
    );
  });

  it("hides local-model fields for the Claude CLI backend, shows them for Ollama", async () => {
    renderWithProviders(<SettingsPage />);
    await waitFor(() => expect(screen.getByText("Copilot model")).toBeInTheDocument());

    // Default backend is the local Claude CLI -> no endpoint/model fields.
    expect(screen.queryByLabelText("Local endpoint")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Copilot backend"), { target: { value: "ollama" } });
    expect(screen.getByLabelText("Local endpoint")).toBeInTheDocument();
    expect(screen.getByLabelText("Model name")).toBeInTheDocument();
  });

  it("saves the full settings object (chosen backend + untouched auto-apply fields)", async () => {
    renderWithProviders(<SettingsPage />);
    await waitFor(() => expect(screen.getByLabelText("Copilot backend")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Copilot backend"), { target: { value: "lmstudio" } });
    fireEvent.change(screen.getByLabelText("Model name"), { target: { value: "qwen3:8b" } });
    fireEvent.click(screen.getByText("Save settings"));

    await waitFor(() => expect(puts.length).toBe(1));
    expect(puts[0].llm_backend).toBe("lmstudio");
    expect(puts[0].llm_model).toBe("qwen3:8b");
    // The 4 auto-apply fields are sent unchanged, so a save never silently resets them.
    expect(puts[0].auto_apply_min_score).toBe(85);
    expect(puts[0].tailor_creativity).toBe("balanced");
  });

  it("persists schedule changes through the settings save", async () => {
    renderWithProviders(<SettingsPage />);
    await waitFor(() => expect(screen.getByLabelText("Schedule cadence")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Schedule cadence"), { target: { value: "weekdays" } });
    fireEvent.change(screen.getByLabelText("Schedule time"), { target: { value: "09:30" } });
    fireEvent.click(screen.getByText("Save settings"));

    await waitFor(() => expect(puts.length).toBe(1));
    expect(puts[0].schedule_cadence).toBe("weekdays");
    expect(puts[0].schedule_time).toBe("09:30");
    expect(puts[0].schedule_enabled).toBe(true);
  });

  it("triggers a discovery run from the Run now button", async () => {
    renderWithProviders(<SettingsPage />);
    await waitFor(() => expect(screen.getByText("Run now (discovery)")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Run now (discovery)"));
    await waitFor(() => {
      const f = global.fetch as Mock;
      expect(
        f.mock.calls.some(
          (c) => String(c[0]).endsWith("/api/runs/discover") && c[1]?.method === "POST",
        ),
      ).toBe(true);
    });
  });

  it("disables the run buttons and shows progress while a run is active", async () => {
    (global.fetch as Mock).mockImplementation(async (url: string) => {
      const u = String(url);
      if (u.includes("/api/runs/status")) {
        return new Response(
          JSON.stringify({
            state: "running", kind: "full",
            started_at: "2026-06-17T08:00:00+00:00", last_run: null,
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ settings: SETTINGS, status: "missing" }), { status: 200 });
    });
    renderWithProviders(<SettingsPage />);
    await waitFor(() => expect(screen.getByText(/Running…/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Running…/ })).toBeDisabled();
  });
});
