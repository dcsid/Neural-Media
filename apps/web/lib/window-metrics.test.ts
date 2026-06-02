import { describe, expect, it } from "vitest";
import { REGION_IDS, type RegionId, type RegionMetrics } from "@shared/types";
import { accumulateRegionMetrics } from "@/lib/window-metrics";

function rm(region_id: RegionId, mean: number, peak: number): RegionMetrics {
  return {
    region_id,
    video_id: "v",
    inference_run_id: "r",
    mean,
    peak,
    sustained: 0,
    timeseries: [],
  };
}

describe("accumulateRegionMetrics", () => {
  it("returns every region zeroed for empty input", () => {
    const out = accumulateRegionMetrics([]);
    for (const id of REGION_IDS) expect(out[id]).toEqual({ mean: 0, peak: 0 });
  });

  it("skips null entries without throwing or skewing the mean (P1.3)", () => {
    const out = accumulateRegionMetrics([
      [rm("v1", 0.4, 0.8)],
      null, // a video whose metrics 404'd / failed to load
      [rm("v1", 0.6, 0.5)],
    ]);
    expect(out.v1.mean).toBeCloseTo(0.5, 5); // (0.4 + 0.6) / 2 — null ignored
    expect(out.v1.peak).toBeCloseTo(0.8, 5); // max peak
  });

  it("ignores unknown region ids", () => {
    const out = accumulateRegionMetrics([
      [
        rm("v1", 1, 1),
        { ...rm("v1", 9, 9), region_id: "bogus" as RegionId },
      ],
    ]);
    expect(out.v1).toEqual({ mean: 1, peak: 1 });
  });

  it("averages per-video means and tracks the max peak per region", () => {
    const out = accumulateRegionMetrics([
      [rm("auditory", 0.2, 0.3), rm("language", 0.1, 0.9)],
      [rm("auditory", 0.8, 0.1)],
    ]);
    expect(out.auditory.mean).toBeCloseTo(0.5, 5); // (0.2 + 0.8) / 2
    expect(out.auditory.peak).toBeCloseTo(0.3, 5); // max(0.3, 0.1)
    expect(out.language.mean).toBeCloseTo(0.1, 5);
    expect(out.language.peak).toBeCloseTo(0.9, 5);
  });
});
