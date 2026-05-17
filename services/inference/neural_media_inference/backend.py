"""Inference backends.

Two backends conform to `InferenceBackend`:

* `MockBackend` — deterministic synthesized activations seeded by
  ``(video_id, seed)``. No GPU, no model weights, no network. Its only job
  is to exercise the contract end-to-end so the rest of the system can be
  built without TRIBE installed.
* ``TribeBackend`` — real TRIBE v2 forward pass. Lives in
  ``backend_tribe.py`` and is gated by ``pip install '.[real]'``. Not
  implemented in this slice — see the worker brief.

Both emit ``np.ndarray`` of shape ``(num_timepoints, 20484)`` in fp32,
values in ``[0, 1]``.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from ._shared import NUM_VERTICES


class InferenceBackend(Protocol):
    """Minimum surface every inference backend must offer."""

    model_id: str
    model_version: str

    def infer(
        self,
        *,
        video_id: str,
        duration_s: float,
        seed: int,
        sample_rate_hz: float,
    ) -> np.ndarray:
        """Return activations of shape ``(num_timepoints, 20484)`` fp32 in ``[0, 1]``."""
        ...


def _derive_seed(video_id: str, seed: int) -> int:
    """Mix ``video_id`` into ``seed`` so two videos with the same caller seed
    still produce different activations. Uses SHA-256 so the mapping is
    stable across Python versions (unlike ``hash()``)."""
    digest = hashlib.sha256(f"{video_id}|{seed}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


class MockBackend:
    """Deterministic placeholder backend.

    Output is the sum of a per-vertex baseline, a low-frequency temporal
    modulation, and a small noise term — all derived from a SHA-256 mix of
    ``(video_id, seed)``. The shape and value range match the real backend
    so downstream code does not need to special-case the mock.

    Range agreement with TribeBackend (verified 2026-05-17 — see
    docs/worker-briefs/ml-inference-status.md "Range alignment"):

    * Both backends produce ``float32`` of shape ``(T, 20484)`` strictly
      inside ``[0, 1]``. The runner's range assertion at runner.py:120
      holds for either.
    * Mock can land on ``1.0`` exactly (``np.clip`` is closed); TRIBE
      post-sigmoid is open. One-ULP gap, not a contract difference.
    * Mock mean ≈ 0.32 with std ≈ 0.22. TRIBE post-sigmoid on z~N(0,1)
      means ≈ 0.50 with std ≈ 0.21. The distributional shift is
      intentional ("real model produces more variation") and
      downstream LUT / aggregation paths are linear in ``[0, 1]``, so
      no special-casing is required.
    """

    model_id: str = "tribe-v2-mock"
    model_version: str = "0.0.0-mock"

    def infer(
        self,
        *,
        video_id: str,
        duration_s: float,
        seed: int,
        sample_rate_hz: float,
    ) -> np.ndarray:
        if duration_s <= 0:
            raise ValueError(f"duration_s must be positive, got {duration_s}")
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be positive, got {sample_rate_hz}")

        num_timepoints = max(1, int(round(duration_s * sample_rate_hz)))
        rng = np.random.default_rng(_derive_seed(video_id, seed))

        spatial = rng.random(NUM_VERTICES, dtype=np.float32)
        phase = rng.random(NUM_VERTICES, dtype=np.float32) * (2.0 * np.pi)
        t_norm = np.linspace(0.0, 1.0, num_timepoints, dtype=np.float32)
        temporal = 0.5 + 0.4 * np.sin(2.0 * np.pi * t_norm[:, None] + phase[None, :])
        noise = rng.random((num_timepoints, NUM_VERTICES), dtype=np.float32) * 0.15

        activations = spatial[None, :] * temporal + noise
        np.clip(activations, 0.0, 1.0, out=activations)
        return activations.astype(np.float32, copy=False)
