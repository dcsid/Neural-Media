"""Region-level aggregation of raw TRIBE activations.

Two surfaces matter here:

1. `REGION_VERTEX_MASKS` — per-region vertex index sets derived from the
   HCP-MMP1 atlas (Glasser et al. 2016, Nature 536:171-178) projected to
   fsaverage5 (the cortical surface TRIBE v2 emits predictions on). Masks
   are disjoint but **do not tile** the 20,484-vertex space — the medial
   wall and parcels outside our 8 curated regions stay unassigned, which
   is the right answer anatomically. The brain-viz worker already encodes
   unassigned vertices as the `255` sentinel (see
   `apps/web/public/brain/README.md`). The committed mapping lives in
   `data/region_masks.json`; regenerate via
   `scripts/build_region_masks.py`.

2. `aggregate_region_metrics`, `downsample_region_means`,
   `keyframe_vertex_snapshots` — pure functions over a ``(T, 20484)``
   activation array.

`sustained` is defined as the 75th-percentile of the per-timepoint
region-mean timeseries — a robust proxy for "the level the signal
sustains above baseline." If this definition changes, update CONTRACTS.md
§5 in the same PR.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import numpy as np

from ._shared import NUM_VERTICES, REGION_IDS

_MASKS_PATH = Path(__file__).resolve().parent / "data" / "region_masks.json"

# Decimal places the standalone URL/gallery scripts round wire timestamps to.
# `run_inference` emits full-precision timestamps (its committed fixtures are
# byte-pinned, so changing precision there would silently diff `make sample`);
# the offline scripts round their compact JSON to this so the two stay
# identical to each other. Single source of truth so they can't drift apart.
WIRE_TIMESTAMP_DECIMALS = 3


def _load_region_masks(path: Path) -> tuple[dict[str, list[int]], dict[str, np.ndarray]]:
    """Load the committed HCP-MMP1 vertex masks. Validates against the
    canonical region set and asserts disjointness; raises at import time if
    the JSON is malformed so a broken atlas can't ship as a silent bug.
    Returns the published (list[int]) and internal (ndarray) forms."""
    if not path.exists():
        raise FileNotFoundError(
            f"region masks not found at {path}. Run "
            f"`python services/inference/scripts/build_region_masks.py` "
            f"to regenerate."
        )
    payload = json.loads(path.read_text())
    if payload.get("num_vertices") != NUM_VERTICES:
        raise ValueError(
            f"region_masks.json declares num_vertices={payload.get('num_vertices')}, "
            f"expected {NUM_VERTICES}"
        )
    masks_raw = payload["masks"]
    if set(masks_raw) != set(REGION_IDS):
        raise ValueError(
            f"region_masks.json keys {sorted(masks_raw)} != REGION_IDS {list(REGION_IDS)}"
        )
    public: dict[str, list[int]] = {}
    private: dict[str, np.ndarray] = {}
    seen: set[int] = set()
    for region_id in REGION_IDS:
        idx = masks_raw[region_id]
        if not idx:
            raise ValueError(f"region '{region_id}' has no vertices")
        arr = np.asarray(idx, dtype=np.int64)
        if arr.min() < 0 or arr.max() >= NUM_VERTICES:
            raise ValueError(
                f"region '{region_id}' has out-of-range vertex indices "
                f"[{int(arr.min())}, {int(arr.max())}]"
            )
        as_set = set(int(v) for v in arr.tolist())
        if len(as_set) != arr.size:
            raise ValueError(f"region '{region_id}' has duplicate vertex indices")
        if not seen.isdisjoint(as_set):
            raise ValueError(f"region '{region_id}' overlaps a previously-loaded region")
        seen |= as_set
        public[region_id] = list(arr.tolist())
        private[region_id] = arr
    return public, private


REGION_VERTEX_MASKS, _REGION_INDEX = _load_region_masks(_MASKS_PATH)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _region_timeseries(activations: np.ndarray, region_id: str) -> np.ndarray:
    """Per-timepoint mean activation within a region. Shape: (T,)."""
    return activations[:, _REGION_INDEX[region_id]].mean(axis=1, dtype=np.float64)


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
        edges = np.linspace(0, T, max_timepoints + 1, dtype=np.int64)
        pooled = np.array(
            [ts[edges[i]:edges[i + 1]].mean() for i in range(max_timepoints)],
            dtype=np.float32,
        )
        out[region_id] = pooled.tolist()
    return out


def downsample_timestamps(
    timestamps: Sequence[float] | np.ndarray,
    *,
    max_timepoints: int,
) -> list[float]:
    """Pool a per-timepoint time axis onto the same bins `downsample_region_means`
    uses, so the wire payload's ``timestamps`` stays the same length as every
    ``region_means`` series.

    Each bin's representative time is the **mean** of the timestamps it covers
    — the midpoint of the block whose activations were mean-pooled — so a chart
    of ``(timestamp, region_mean)`` pairs lines up. Returns a list of length
    ``min(len(timestamps), max_timepoints)``; a series already at or under the
    cap is returned unchanged (full precision, byte-stable).

    Mirrors the ``np.linspace(0, T, max_timepoints + 1)`` edges in
    `downsample_region_means` exactly, so the two always agree on length.
    """
    if max_timepoints <= 0:
        raise ValueError(f"max_timepoints must be positive, got {max_timepoints}")
    ts = np.asarray(timestamps, dtype=np.float64)
    if ts.ndim != 1:
        raise ValueError(f"timestamps must be 1-D, got shape {ts.shape}")
    T = ts.shape[0]
    if T <= max_timepoints:
        return ts.tolist()
    edges = np.linspace(0, T, max_timepoints + 1, dtype=np.int64)
    return [float(ts[edges[i]:edges[i + 1]].mean()) for i in range(max_timepoints)]


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
