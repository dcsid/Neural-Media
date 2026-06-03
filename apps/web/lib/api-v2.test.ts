import { describe, expect, it, vi } from "vitest";
import { REGION_IDS } from "@shared/types";
import {
  ApiV2Error,
  createUrlJob,
  fetchActivation,
  getJob,
  looksLikeYouTubeUrl,
  MAX_SEGMENT_SEC,
  validateSegment,
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
    await expect(
      createUrlJob("https://www.youtube.com/watch?v=abc", {
        startSec: 0,
        endSec: 30,
      }),
    ).rejects.toBe(abort);
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
    const err = await createUrlJob("https://www.youtube.com/watch?v=abc", {
      startSec: 0,
      endSec: 30,
    }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiV2Error);
    expect(err.kind).toBe("offline");
  });
});

// --- looksLikeYouTubeUrl -----------------------------------------------------

describe("looksLikeYouTubeUrl", () => {
  it.each([
    ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", true],
    ["https://youtube.com/watch?v=abc", true],
    ["https://m.youtube.com/watch?v=abc", true],
    ["https://youtu.be/dQw4w9WgXcQ", true],
    ["https://www.youtube.com/shorts/abc123", true],
    ["http://youtube.com/watch?v=abc", true],
    ["", false],
    ["not a url", false],
    ["https://www.youtube.com/watch", false], // no v param
    ["https://youtu.be/", false], // no id
    ["https://www.youtube.com/feed/subscriptions", false],
    ["https://example.com/watch?v=abc", false],
    ["ftp://youtube.com/watch?v=abc", false],
    // Lookalike hosts must be rejected.
    ["https://notyoutube.com/watch?v=abc", false],
    ["https://youtube.com.attacker.net/watch?v=abc", false],
    ["https://www.tiktok.com/@a/video/1", false],
  ])("%s -> %s", (input, expected) => {
    expect(looksLikeYouTubeUrl(input)).toBe(expected);
  });
});

// --- validateSegment --------------------------------------------------------

describe("validateSegment (CONTRACTS.md §13.2)", () => {
  it.each([
    [0, 30, null],
    [12, 78, null],
    [0, MAX_SEGMENT_SEC, null], // exactly the cap is allowed
    [0, MAX_SEGMENT_SEC + 0.1, "segment_too_long"],
    [10, 10, "bad_segment"], // start == end
    [20, 10, "bad_segment"], // start > end
    [-1, 30, "bad_segment"], // negative start
    [Number.NaN, 30, "bad_segment"], // empty input parses to NaN
    [0, Number.NaN, "bad_segment"],
  ])("(%s, %s) -> %s", (start, end, expected) => {
    expect(validateSegment(start, end)).toBe(expected);
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
