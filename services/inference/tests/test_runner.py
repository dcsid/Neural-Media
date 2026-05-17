"""Smoke tests for run_inference — NPZ + sidecar, reproducibility envelope."""

from __future__ import annotations

import json

import numpy as np

from neural_media_inference import MockBackend, run_inference
from neural_media_inference._shared import NUM_VERTICES, REGION_IDS


def test_run_writes_npz_and_sidecar(tmp_path):
    artifacts = run_inference(
        video_id="vid-1",
        duration_s=12.0,
        seed=3,
        sample_rate_hz=1.5,
        activations_dir=tmp_path,
        backend=MockBackend(),
    )
    assert artifacts.activation_path.exists()
    assert artifacts.sidecar_path.exists()

    arr = np.load(artifacts.activation_path)["activations"]
    assert arr.dtype == np.float32
    assert arr.shape[1] == NUM_VERTICES
    assert arr.shape[0] == artifacts.activation_payload.num_timepoints

    sidecar = json.loads(artifacts.sidecar_path.read_text())
    assert sidecar["inference_run_id"] == artifacts.inference_run.id
    assert sidecar["video_id"] == "vid-1"
    assert sidecar["model_id"] == "tribe-v2-mock"
    assert sidecar["seed"] == 3
    assert sidecar["num_vertices"] == NUM_VERTICES


def test_reproducibility_envelope_is_complete(tmp_path):
    artifacts = run_inference(
        video_id="vid-2",
        duration_s=8.0,
        seed=11,
        sample_rate_hz=2.0,
        activations_dir=tmp_path,
    )
    run = artifacts.inference_run
    assert run.model_id == "tribe-v2-mock"
    assert run.model_version
    assert run.seed == 11
    assert run.created_at.tzinfo is not None  # UTC, not naive
    p = run.params_json
    # CONTRACTS.md §8: model id+version live on InferenceRun itself; params
    # must carry seed-relevant preprocessing + config hash + timestamp.
    assert "preprocessing" in p
    assert {"video_resolution", "video_fps", "audio_sample_rate_hz"} <= set(p["preprocessing"])
    assert "config_hash" in p and len(p["config_hash"]) == 64
    assert "created_at_utc" in p
    assert p["sample_rate_hz"] == 2.0
    assert p["duration_s"] == 8.0


def test_region_metrics_one_per_region(tmp_path):
    artifacts = run_inference(
        video_id="vid-3",
        duration_s=10.0,
        seed=5,
        sample_rate_hz=1.5,
        activations_dir=tmp_path,
    )
    ids = [m.region_id for m in artifacts.region_metrics]
    assert ids == list(REGION_IDS)
    for m in artifacts.region_metrics:
        assert m.video_id == "vid-3"
        assert m.inference_run_id == artifacts.inference_run.id
        assert len(m.timeseries) == artifacts.activation_payload.num_timepoints


def test_activation_payload_has_keyframes_and_region_means(tmp_path):
    artifacts = run_inference(
        video_id="vid-4",
        duration_s=200.0,
        seed=1,
        sample_rate_hz=1.5,
        activations_dir=tmp_path,
        max_wire_timepoints=60,
        num_keyframes=4,
    )
    payload = artifacts.activation_payload
    assert set(payload.region_means) == set(REGION_IDS)
    for ts in payload.region_means.values():
        assert len(ts) == 60
    assert len(payload.keyframe_vertices) == 4
    for vec in payload.keyframe_vertices.values():
        assert len(vec) == NUM_VERTICES


def test_compress_false_writes_uncompressed_and_round_trips(tmp_path):
    """`compress=False` is the demo/mock-mode escape hatch — must produce a
    valid NPZ that still round-trips through np.load, just larger on disk."""
    kwargs = dict(
        video_id="vid-compress",
        duration_s=12.0,
        seed=4,
        sample_rate_hz=1.5,
        backend=MockBackend(),
    )
    fast = run_inference(activations_dir=tmp_path / "fast", compress=False, **kwargs)
    small = run_inference(activations_dir=tmp_path / "small", compress=True, **kwargs)

    fast_arr = np.load(fast.activation_path)["activations"]
    small_arr = np.load(small.activation_path)["activations"]
    assert np.array_equal(fast_arr, small_arr)
    assert fast_arr.dtype == np.float32

    # Uncompressed NPZ should be strictly larger than compressed for a
    # smooth fp32 signal. This is the regression guard: if the savez
    # dispatch ever flips, file sizes invert.
    assert fast.activation_path.stat().st_size > small.activation_path.stat().st_size


def test_fixture_overrides_make_run_deterministic(tmp_path):
    from datetime import datetime, timezone
    fixed_run_id = "00000000-0000-0000-0000-000000000abc"
    fixed_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    kwargs = dict(
        video_id="vid-5",
        duration_s=6.0,
        seed=9,
        sample_rate_hz=1.5,
        activations_dir=tmp_path,
        run_id=fixed_run_id,
        created_at=fixed_ts,
    )
    a = run_inference(**kwargs)
    b = run_inference(**kwargs)
    assert a.inference_run.id == fixed_run_id == b.inference_run.id
    assert a.inference_run.created_at == b.inference_run.created_at == fixed_ts
    assert a.inference_run.model_dump_json() == b.inference_run.model_dump_json()
