import { describe, expect, it, vi } from "vitest";
import { REGION_IDS } from "@shared/types";
import {
  ApiV2Error,
  createUrlJob,
  fetchActivation,
  getJob,
  looksLikeTikTokUrl,
  type ActivationPayload,
} from "@/lib/api-v2";

// --- helpers ---------------------------------------------------------------

function mockFetchReject(err: unknown): void {
  vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockRejectedValue(err));
}

function mockFetchJson(payload: unknown, init?: { ok?: boolean; status?: number }) {
  const ok = init?.ok ?? true;
  const status = init?.status ?? (ok ? 200 : 500);
  const body = {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: async () => payload,
    clone() {
      return this;
    },
    text: async () => JSON.stringify(payload),
  };
  vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(body as unknown as Response));
}

function validActivation(): ActivationPayload {
  const timestamps = [0, 0.5, 1];
  const series = [0.1, 0.2, 0.3];
  const byRegion = Object.fromEntries(
    REGION_IDS.map((r) => [r, [...series]]),
  ) as ActivationPayload["byRegion"];
  return { videoDurationSec: 1.5, timestamps, byRegion, modelVersion: "tribe-v2-mock" };
}

// --- AbortError propagation (P1.4) -----------------------------------------

describe("fetch wrappers re-throw AbortError before wrapping (P1.4)", () => {
  it("re-throws a DOMException AbortError untouched", async () => {
    const abort = new DOMException("Aborted", "AbortError");
    mockFetchReject(abort);
    await expect(createUrlJob("https://www.tiktok.com/@a/video/1")).rejects.toBe(
      abort,
    );
  });

  it("re-throws a plain-Error AbortError untouched (non-DOMException runtimes)", async () => {
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    mockFetchReject(abort);
    // getJob exercises the GET wrapper; the regression is that this is NOT
    // re-wrapped as an offline ApiV2Error just because it isn't a DOMException.
    await expect(getJob("job-1")).rejects.toBe(abort);
  });

  it("still wraps a genuine network failure as ApiV2Error offline", async () => {
    mockFetchReject(new TypeError("Failed to fetch"));
    const err = await createUrlJob("https://www.tiktok.com/@a/video/1").catch(
      (e) => e,
    );
    expect(err).toBeInstanceOf(ApiV2Error);
    expect(err.kind).toBe("offline");
  });
});

// --- looksLikeTikTokUrl -----------------------------------------------------

describe("looksLikeTikTokUrl", () => {
  it.each([
    ["https://www.tiktok.com/@nasa/video/123", true],
    ["https://tiktok.com/@x", true],
    ["https://vm.tiktok.com/abc", true],
    ["http://m.tiktok.com/v/1", true],
    ["", false],
    ["not a url", false],
    ["https://example.com/@x", false],
    ["ftp://tiktok.com/x", false],
    // Suffix check must not be fooled by lookalike hosts.
    ["https://eviltiktok.com/@x", false],
    ["https://tiktok.com.attacker.net/@x", false],
  ])("%s -> %s", (input, expected) => {
    expect(looksLikeTikTokUrl(input)).toBe(expected);
  });
});

// --- fetchActivation validation --------------------------------------------

describe("fetchActivation validates the payload shape", () => {
  it("returns a well-formed activation", async () => {
    const payload = validActivation();
    mockFetchJson(payload);
    await expect(fetchActivation("https://cdn/x.json")).resolves.toEqual(payload);
  });

  it("rejects when videoDurationSec is missing", async () => {
    const { videoDurationSec: _drop, ...rest } = validActivation();
    mockFetchJson(rest);
    await expect(fetchActivation("https://cdn/x.json")).rejects.toMatchObject({
      kind: "validation",
    });
  });

  it("rejects when a region series length != timestamps length", async () => {
    const payload = validActivation();
    payload.byRegion[REGION_IDS[0]] = [0.1, 0.2]; // length 2 vs timestamps 3
    mockFetchJson(payload);
    await expect(fetchActivation("https://cdn/x.json")).rejects.toMatchObject({
      kind: "validation",
    });
  });

  it("rejects a non-ok HTTP response as an http error", async () => {
    mockFetchJson({}, { ok: false, status: 502 });
    await expect(fetchActivation("https://cdn/x.json")).rejects.toMatchObject({
      kind: "http",
      status: 502,
    });
  });
});
