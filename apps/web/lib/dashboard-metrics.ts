import type { AggregateReport, InferenceRun } from "@shared/types";

// Mean predicted activation across all cortical regions in the aggregate —
// the single "Mean activation" stat on the dashboard. Averages the per-region
// means; returns 0 for an empty by_region map rather than NaN.
export function meanActivationAcrossRegions(
  byRegion: AggregateReport["by_region"],
): number {
  const buckets = Object.values(byRegion);
  if (buckets.length === 0) return 0;
  const sum = buckets.reduce((acc, b) => acc + (b?.mean ?? 0), 0);
  return sum / buckets.length;
}

// True when every completed inference run came from a mock backend (model_id
// prefixed "tribe-v2-mock"). False on an empty list — no runs means there is
// nothing to label as mock. Matches MockModeBadge's contract check, and gates
// the dashboard's URL-hiding behaviour.
export function isAllMockRuns(runs: InferenceRun[]): boolean {
  return (
    runs.length > 0 &&
    runs.every((r) => r.model_id.startsWith("tribe-v2-mock"))
  );
}
