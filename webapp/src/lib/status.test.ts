import { describe, it, expect } from "vitest";
import { statusColor, scoreTone, STATUS_ORDER } from "./status";

describe("status helpers", () => {
  it("maps known statuses to a css var", () => {
    expect(statusColor("Ready to apply")).toBe("var(--status-ready)");
    expect(statusColor("Recruiter reply")).toBe("var(--status-reply)");
  });
  it("falls back to neutral for unknown", () => {
    expect(statusColor("Nonsense")).toBe("var(--status-neutral)");
  });
  it("scoreTone buckets by threshold", () => {
    expect(scoreTone(90)).toBe("high");
    expect(scoreTone(75)).toBe("mid");
    expect(scoreTone(50)).toBe("low");
    expect(scoreTone(null)).toBe("none");
  });
  it("STATUS_ORDER is the canonical pipeline order", () => {
    expect(STATUS_ORDER[0]).toBe("Discovered");
    expect(STATUS_ORDER).toContain("Offer");
  });
});
