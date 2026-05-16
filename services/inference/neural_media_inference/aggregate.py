"""Region-level aggregation of raw TRIBE activations.

Two surfaces matter here:

1. `REGION_VERTEX_MASKS` — disjoint per-region vertex index sets, keyed by
   region id from `shared.schemas.REGION_IDS`. The masks committed in this
   slice are **placeholder contiguous slabs**, not real anatomy. They
   exist so the contract is exercisable today; an HCP MMP1 / Glasser
   parcel table MUST replace them before any external demo (see
   docs/worker-briefs/ml-inference.md §3).
2. `aggregate_region_metrics`, `downsample_region_means`,
   `keyframe_vertex_snapshots` — pure functions over a ``(T, 20484)``
   activation array.

`sustained` is defined as the 75th-percentile of the per-timepoint
region-mean timeseries — a robust proxy for "the level the signal
sustains above baseline." If this definition changes, update CONTRACTS.md
§5 in the same PR.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from ._shared import NUM_VERTICES, REGION_IDS


# ---------------------------------------------------------------------------
# Placeholder region masks. Disjoint contiguous slabs covering all 20,484
# vertices. Sizes are not anatomically meaningful — they only ensure each
# region has enough vertices to produce a non-degenerate timeseries.
# ---------------------------------------------------------------------------

_PLACEHOLDER_REGION_RANGES: dict[str, tuple[int, int]] = {
    "v1":       (0,     3000),
    "v2":       (3000,  5500),
    "v3":       (5500,  7500),
    "v4":       (7500,  9100),
    "auditory": (9100,  11100),
    "language": (11100, 14300),
    "ffa":      (14300, 16800),
    "vwfa":     (16800, NUM_VERTICES),
}

# Sanity: every canonical region has a mask, and masks tile [0, NUM_VERTICES).
assert set(_PLACEHOLDER_REGION_RANGES) == set(REGION_IDS), (
    "REGION_VERTEX_MASKS must cover exactly shared.schemas.REGION_IDS"
)
_covered = 0
for _lo, _hi in _PLACEHOLDER_REGION_RANGES.values():
    assert 0 <= _lo < _hi <= NUM_VERTICES
    _covered += _hi - _lo
assert _covered == NUM_VERTICES, "placeholder masks must tile the cortex"
del _lo, _hi, _covered


REGION_VERTEX_MASKS: dict[str, list[int]] = {
    region_id: list(range(lo, hi))
    for region_id, (lo, hi) in _PLACEHOLDER_REGION_RANGES.items()
}


def _region_slice(region_id: str) -> slice:
    """Internal fast path. The published `REGION_VERTEX_MASKS` is the API
    contract; for hot paths we use the contiguous slice directly so we
    don't materialize a 20k-element index list per call."""
    lo, hi = _PLACEHOLDER_REGION_RANGES[region_id]
    return slice(lo, hi)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _region_timeseries(activations: np.ndarray, region_id: str) -> np.ndarray:
    """Per-timepoint mean activation within a region. Shape: (T,)."""
    return activations[:, _region_slice(region_id)].mean(axis=1, dtype=np.float64)


def aggregate_region_metrics(
    activations: np.ndarray,
    *,
    video_id: str,
    inference_run_id: str,
) -> list[dict]:
    """Reduce a ``(T, 20484)`` activation array to one `RegionMetrics` row
    per canonical region.

    Returns dicts (not Pydantic models) so the caller decides when to
    validate; matches CONTRACTS.md §5.
    """
    if activations.ndim != 2 or activations.shape[1] != NUM_VERTICES:
        raise ValueError(
            f"activations must be (T, {NUM_VERTICES}); got {activations.shape}"
        )

    rows: list[dict] = []
    for region_id in REGION_IDS:
        ts = _region_timeseries(activations, region_id)
        rows.append({
            "region_id": region_id,
            "video_id": video_id,
            "inference_run_id": inference_run_id,
            "mean": float(ts.mean()),
            "peak": float(ts.max()),
            "sustained": float(np.percentile(ts, 75)),
            "timeseries": ts.astype(np.float32).tolist(),
        })
    return rows


def downsample_region_means(
    activations: np.ndarray,
    *,
    max_timepoints: int,
) -> dict[str, list[float]]:
    """Per-region downsampled timeseries for wire-format `ActivationOutput`.

    Each output list has length ``min(T, max_timepoints)``. When
    downsampling, contiguous blocks are mean-pooled — never decimated —
    so peaks aren't accidentally dropped.
    """
    if max_timepoints <= 0:
        raise ValueError(f"max_timepoints must be positive, got {max_timepoints}")

    out: dict[str, list[float]] = {}
    T = activations.shape[0]
    for region_id in REGION_IDS:
        ts = _region_timeseries(activations, region_id)
        if T <= max_timepoints:
            out[region_id] = ts.astype(np.float32).tolist()
            continue
        # Mean-pool into max_timepoints contiguous blocks.
        edges = np.linspace(0, T, max_timepoints + 1, dtype=np.int64)
        pooled = np.array(
            [ts[edges[i]:edges[i + 1]].mean() for i in range(max_timepoints)],
            dtype=np.float32,
        )
        out[region_id] = pooled.tolist()
    return out


def keyframe_vertex_snapshots(
    activations: np.ndarray,
    *,
    num_keyframes: int,
    timestamps: Sequence[float] | None = None,
) -> dict[str, list[float]]:
    """Pick ``num_keyframes`` evenly-spaced timepoints and emit the full
    20,484-dim vertex vector at each. Keyed by the timestamp formatted as
    a fixed-precision string (or by frame index if ``timestamps`` is None),
    so the frontend can render the brain mesh at a few representative
    moments without ever pulling the full ``(T, 20484)`` tensor.
    """
    if num_keyframes <= 0:
        raise ValueError(f"num_keyframes must be positive, got {num_keyframes}")
    T = activations.shape[0]
    k = min(num_keyframes, T)
    indices = np.linspace(0, T - 1, k, dtype=np.int64)

    snapshots: dict[str, list[float]] = {}
    for idx in indices:
        if timestamps is not None:
            key = f"{float(timestamps[idx]):.3f}"
        else:
            key = str(int(idx))
        snapshots[key] = activations[idx].astype(np.float32).tolist()
    return snapshots
