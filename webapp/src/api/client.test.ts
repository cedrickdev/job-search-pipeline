import { describe, it, expect, vi, beforeEach } from "vitest";
import { apiGet, apiPost, ApiError } from "./client";

describe("api client", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("GET returns parsed json", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    ));
    expect(await apiGet<{ ok: boolean }>("/api/x")).toEqual({ ok: true });
  });

  it("POST sends json body and parses response", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ id: 1 }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const out = await apiPost<{ id: number }>("/api/y", { a: 1 });
    expect(out).toEqual({ id: 1 });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ a: 1 });
  });

  it("throws ApiError with status and detail on non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ detail: "nope" }), { status: 409 }),
    ));
    await expect(apiPost("/api/z", {})).rejects.toMatchObject({
      status: 409,
      detail: "nope",
    });
  });
});
