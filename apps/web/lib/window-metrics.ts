import { REGION_IDS, type RegionId, type RegionMetrics } from "@shared/types";

export interface RegionStat {
  mean: number;
  peak: number;
}

// Accumulate mean + peak per cortical region across a set of per-video
// metric lists, as used by the windowed comparison view.
//
// `null` entries — videos whose metrics fetch failed or 404'd (no completed
// inference yet) — are skipped rather than failing the whole comparison.
// Unknown region_ids are ignored. The reported `mean` is the average of the
// per-video means across the videos that contributed each region; `peak` is
// the max per-video peak. Always returns an entry for every REGION_ID so
// downstream rendering can index without guarding.
export function accumulateRegionMetrics(
  metricsLists: ReadonlyArray<RegionMetrics[] | null>,
): Record<RegionId, RegionStat> {
  const byRegion = {} as Record<RegionId, RegionStat>;
  const sums = {} as Record<RegionId, number>;
  const counts = {} as Record<RegionId, number>;
  for (const id of REGION_IDS) {
    byRegion[id] = { mean: 0, peak: 0 };
    sums[id] = 0;
    counts[id] = 0;
  }

  for (const metrics of metricsLists) {
    if (!metrics) continue;
    for (const m of metrics) {
      const id = m.region_id;
      if (!(id in byRegion)) continue;
      sums[id] += m.mean;
      counts[id] += 1;
      if (m.peak > byRegion[id].peak) byRegion[id].peak = m.peak;
    }
  }

  for (const id of REGION_IDS) {
    if (counts[id] > 0) byRegion[id].mean = sums[id] / counts[id];
  }

  return byRegion;
}
