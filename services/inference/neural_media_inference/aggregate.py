"""Region-level aggregation of raw TRIBE activations — STUB.

Owner: ml-inference worker. See docs/worker-briefs/ml-inference.md.

Required exports:
  - `REGION_VERTEX_MASKS: dict[str, list[int]]` — disjoint per-region
    vertex index sets, keyed by region id from `shared.schemas.REGION_IDS`.
    Replace placeholder masks with parcels from a published atlas before
    any external demo.
  - `aggregate_region_metrics(activations, *, video_id, inference_run_id)`
    returning a list of dicts matching `RegionMetrics` (CONTRACTS.md §5).
  - `downsample_region_means(activations, *, max_timepoints)` for the
    wire-format `ActivationOutput.region_means` (CONTRACTS.md §4).
  - `keyframe_vertex_snapshots(activations, *, num_keyframes)` for the
    3D viz keyframes consumed by the brain-viz worker.
"""

from __future__ import annotations

# TODO(ml-inference): populate REGION_VERTEX_MASKS and aggregation helpers.
REGION_VERTEX_MASKS: dict[str, list[int]] = {}
