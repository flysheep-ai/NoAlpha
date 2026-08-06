import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("console api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("serializes multiple evidence ids for claim support lookup", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toContain("evidence_id=E1");
      expect(url).toContain("evidence_id=E2");
      return new Response(JSON.stringify({ items: [], total: 0, limit: 20, offset: 0 }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.evidence("INV-1", { evidence_id: ["E1", "E2"], limit: 20 });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("surfaces backend errors instead of hiding provider failures", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Market data provider failed: HTTP 401", { status: 500 })));
    await expect(api.providers()).rejects.toThrow("Market data provider failed");
  });
});
