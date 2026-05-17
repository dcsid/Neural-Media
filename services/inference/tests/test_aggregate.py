"""Smoke tests for region aggregation."""

from __future__ import annotations

import numpy as np
import pytest

from neural_media_inference import (
    REGION_VERTEX_MASKS,
    aggregate_region_metrics,
    downsample_region_means,
    keyframe_vertex_snapshots,
)
from neural_media_inference._shared import NUM_VERTICES, REGION_IDS


def _fake_activations(T: int = 40, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((T, NUM_VERTICES), dtype=np.float32)


def test_masks_cover_canonical_regions():
    assert set(REGION_VERTEX_MASKS) == set(REGION_IDS)


def test_masks_are_disjoint():
    """HCP-MMP1 parcels are anatomically disjoint, so any two regions in our
    8-region set must be too. We do NOT assert tiling — the medial wall and
    cortex outside our curated regions stay unassigned by design (see the
    aggregate.py docstring and apps/web/public/brain/README.md)."""
    seen: set[int] = set()
    for region_id, idx_list in REGION_VERTEX_MASKS.items():
        idx_set = set(idx_list)
        assert len(idx_set) == len(idx_list), f"{region_id} has duplicates"
        assert seen.isdisjoint(idx_set), f"{region_id} overlaps a prior region"
        seen |= idx_set


def test_masks_do_not_tile_cortex():
    """Sanity: with real anatomy in place, ~10–30% of vertices belong to one
    of the 8 regions. The rest are medial wall + motor/parietal/PFC parcels
    we didn't include. This test will fail if someone reverts to placeholder
    slabs — that regression is exactly what we're guarding against."""
    total = sum(len(v) for v in REGION_VERTEX_MASKS.values())
    assert total < NUM_VERTICES, "real atlas masks must not tile the cortex"
    assert total >= 1_000, f"suspiciously sparse coverage: {total} verts"


def test_each_region_has_vertices():
    for region_id in REGION_IDS:
        assert len(REGION_VERTEX_MASKS[region_id]) > 0, f"{region_id} is empty"


def test_aggregate_returns_one_row_per_region():
    acts = _fake_activations()
    rows = aggregate_region_metrics(acts, video_id="v1", inference_run_id="r1")
    assert [r["region_id"] for r in rows] == list(REGION_IDS)


def test_aggregate_row_matches_contract():
    acts = _fake_activations(T=30)
    rows = aggregate_region_metrics(acts, video_id="vid-x", inference_run_id="run-x")
    row = rows[0]
    assert row["video_id"] == "vid-x"
    assert row["inference_run_id"] == "run-x"
    assert isinstance(row["mean"], float)
    assert isinstance(row["peak"], float)
    assert isinstance(row["sustained"], float)
    assert len(row["timeseries"]) == 30
    assert row["mean"] <= row["peak"]
    assert row["mean"] >= 0.0 and row["peak"] <= 1.0


def test_aggregate_rejects_wrong_vertex_count():
    bad = np.zeros((5, NUM_VERTICES - 1), dtype=np.float32)
    with pytest.raises(ValueError):
        aggregate_region_metrics(bad, video_id="v", inference_run_id="r")


def test_downsample_caps_timepoints_and_keeps_short_series():
    acts = _fake_activations(T=500)
    pooled = downsample_region_means(acts, max_timepoints=50)
    for region_id, series in pooled.items():
        assert len(series) == 50
        assert all(0.0 <= v <= 1.0 for v in series)

    short = _fake_activations(T=10)
    pooled_short = downsample_region_means(short, max_timepoints=50)
    assert all(len(v) == 10 for v in pooled_short.values())


def test_keyframes_evenly_spaced_and_full_width():
    acts = _fake_activations(T=60)
    snaps = keyframe_vertex_snapshots(acts, num_keyframes=5)
    assert len(snaps) == 5
    for vec in snaps.values():
        assert len(vec) == NUM_VERTICES


def test_keyframe_keys_are_timestamps_when_supplied():
    acts = _fake_activations(T=10)
    timestamps = [i / 1.5 for i in range(10)]
    snaps = keyframe_vertex_snapshots(acts, num_keyframes=3, timestamps=timestamps)
    parsed = sorted(float(k) for k in snaps)
    assert parsed[0] == pytest.approx(timestamps[0])
    assert parsed[-1] == pytest.approx(timestamps[-1])
