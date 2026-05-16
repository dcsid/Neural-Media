"""Smoke tests for MockBackend — shape, dtype, range, determinism."""

from __future__ import annotations

import numpy as np

from neural_media_inference import MockBackend
from neural_media_inference._shared import NUM_VERTICES


def test_output_shape_and_dtype():
    out = MockBackend().infer(
        video_id="vid-abc", duration_s=20.0, seed=7, sample_rate_hz=1.5
    )
    assert out.dtype == np.float32
    assert out.ndim == 2
    assert out.shape[1] == NUM_VERTICES
    assert out.shape[0] == int(round(20.0 * 1.5))


def test_output_in_unit_range():
    out = MockBackend().infer(
        video_id="vid-abc", duration_s=10.0, seed=1, sample_rate_hz=2.0
    )
    assert float(out.min()) >= 0.0
    assert float(out.max()) <= 1.0


def test_determinism_same_inputs_byte_identical():
    a = MockBackend().infer(
        video_id="vid-abc", duration_s=8.0, seed=42, sample_rate_hz=1.5
    )
    b = MockBackend().infer(
        video_id="vid-abc", duration_s=8.0, seed=42, sample_rate_hz=1.5
    )
    assert np.array_equal(a, b)


def test_different_video_id_differs():
    seed = 7
    a = MockBackend().infer(
        video_id="vid-a", duration_s=8.0, seed=seed, sample_rate_hz=1.5
    )
    b = MockBackend().infer(
        video_id="vid-b", duration_s=8.0, seed=seed, sample_rate_hz=1.5
    )
    assert not np.array_equal(a, b)


def test_different_seed_differs():
    vid = "vid-abc"
    a = MockBackend().infer(
        video_id=vid, duration_s=8.0, seed=1, sample_rate_hz=1.5
    )
    b = MockBackend().infer(
        video_id=vid, duration_s=8.0, seed=2, sample_rate_hz=1.5
    )
    assert not np.array_equal(a, b)


def test_model_id_is_mock():
    backend = MockBackend()
    assert backend.model_id == "tribe-v2-mock"
    assert "mock" in backend.model_version
