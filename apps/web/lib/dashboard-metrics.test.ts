import { describe, expect, it } from "vitest";
import {
  REGION_IDS,
  type AggregateReport,
  type InferenceRun,
  type RegionId,
} from "@shared/types";
import {
  isAllMockRuns,
  meanActivationAcrossRegions,
} from "@/lib/dashboard-metrics";

function byRegion(
  means: Partial<Record<RegionId, number>>,
): AggregateReport["by_region"] {
  return Object.fromEntries(
    REGION_IDS.map((r) => [r, { mean: means[r] ?? 0, peak: 0 }]),
  ) as AggregateReport["by_region"];
}

function run(over: Partial<InferenceRun> = {}): InferenceRun {
  return {
    id: "r",
    video_id: "v",
    model_id: "tribe-v2-mock",
    model_version: "1",
    seed: 0,
    params_json: {},
    created_at: "2026-01-01T00:00:00Z",
    activation_path: "p",
    status: "complete",
    ...over,
  };
}

describe("meanActivationAcrossRegions", () => {
  it("averages the per-region means across all regions", () => {
    // 8 regions; only two non-zero → (0.8 + 0.2) / 8.
    expect(
      meanActivationAcrossRegions(byRegion({ v1: 0.8, auditory: 0.2 })),
    ).toBeCloseTo(0.125, 5);
  });

  it("returns 0 (not NaN) for an empty by_region map", () => {
    expect(
      meanActivationAcrossRegions({} as AggregateReport["by_region"]),
    ).toBe(0);
  });
});

describe("isAllMockRuns", () => {
  it("is true when every run is a tribe-v2-mock build", () => {
    expect(
      isAllMockRuns([run(), run({ model_id: "tribe-v2-mock-gallery" })]),
    ).toBe(true);
  });

  it("is false when any run is real", () => {
    expect(isAllMockRuns([run(), run({ model_id: "tribe-v2" })])).toBe(false);
  });

  it("is false for an empty list — nothing to label as mock", () => {
    expect(isAllMockRuns([])).toBe(false);
  });
});
