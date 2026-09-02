import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MicButton } from "./MicButton";

vi.mock("../lib/recorder", () => ({
  recordClip: vi.fn(async () => new Blob(["audio"], { type: "audio/webm" })),
}));

describe("MicButton", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ text: "spoken words" }), { status: 200 }),
    ));
  });

  it("records, posts to /api/transcribe, and calls onTranscript with the text", async () => {
    const onTranscript = vi.fn();
    render(<MicButton onTranscript={onTranscript} />);
    await userEvent.click(screen.getByRole("button", { name: /record|mic|voice/i }));
    await waitFor(() => expect(onTranscript).toHaveBeenCalledWith("spoken words"));
    const fetchMock = global.fetch as ReturnType<typeof vi.fn>;
    expect(fetchMock.mock.calls.some(
      (c) => typeof c[0] === "string" && c[0].includes("/api/transcribe"),
    )).toBe(true);
  });
});
