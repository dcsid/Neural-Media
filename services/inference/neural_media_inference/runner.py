"""Inference orchestration.

`run_inference` is the only entry point api-orchestrator imports. It:

* runs the backend,
* writes the raw ``(T, 20484)`` tensor to disk as ``{run_id}.npz``,
* writes the `ActivationSidecar` JSON next to it,
* aggregates region metrics,
* builds the wire-format `ActivationOutput`,
* assembles the `InferenceRun` row with the full reproducibility envelope
  (CONTRACTS.md §8).

The reproducibility envelope is non-negotiable: every `InferenceRun.params_json`
carries the model id + version, seed, preprocessing params, TRIBE config
hash, and a UTC timestamp. If any of these are dropped, the rest of the
project is not doing science.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ._shared import (
    NUM_VERTICES,
    ActivationOutput,
    ActivationSidecar,
    InferenceRun,
    RegionMetrics,
)
from .aggregate import (
    aggregate_region_metrics,
    downsample_region_means,
    downsample_timestamps,
    keyframe_vertex_snapshots,
)
from .backend import InferenceBackend, MockBackend

_log = logging.getLogger(__name__)


DEFAULT_PREPROCESSING_PARAMS: dict[str, Any] = {
    "video_resolution": "224x224",
    "video_fps": 8,
    "audio_sample_rate_hz": 16000,
}

DEFAULT_MAX_TIMEPOINTS_WIRE = 120
DEFAULT_NUM_KEYFRAMES = 5


@dataclass(frozen=True)
class RunArtifacts:
    """Everything one inference run produces. Returned by `run_inference`.

    The api-orchestrator persists `inference_run` + `region_metrics`,
    serves `activation_payload` over the wire, and keeps the file paths
    around for the rare case the user wants the full tensor.
    """

    inference_run: InferenceRun
    region_metrics: list[RegionMetrics]
    activation_payload: ActivationOutput
    activation_path: Path
    sidecar_path: Path


def _config_hash(*, model_id: str, model_version: str, seed: int, params: dict[str, Any]) -> str:
    """Hash of the reproducibility-relevant inputs. Two runs with the same
    hash should be byte-equivalent up to backend non-determinism."""
    payload = json.dumps(
        {"model_id": model_id, "model_version": model_version, "seed": seed, "params": params},
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run_inference(
    *,
    video_id: str,
    duration_s: float,
    seed: int,
    sample_rate_hz: float,
    activations_dir: Path | str,
    backend: InferenceBackend | None = None,
    extra_params: dict[str, Any] | None = None,
    max_wire_timepoints: int = DEFAULT_MAX_TIMEPOINTS_WIRE,
    num_keyframes: int = DEFAULT_NUM_KEYFRAMES,
    run_id: str | None = None,
    created_at: datetime | None = None,
    compress: bool = True,
) -> RunArtifacts:
    backend = backend if backend is not None else MockBackend()
    activations_dir = Path(activations_dir)
    activations_dir.mkdir(parents=True, exist_ok=True)

    # run_id / created_at overrides exist for fixture regeneration only
    # (scripts/build_sample_outputs.py). Production callers leave them None.
    run_id = run_id if run_id is not None else str(uuid.uuid4())
    created_at = created_at if created_at is not None else datetime.now(timezone.utc)

    # INFO-level breadcrumbs so a real GPU run isn't silent for minutes while
    # TRIBE chews through a clip (the heavy weight-load happens in the
    # backend's constructor; this is the forward pass).
    _log.info(
        "event=infer-start video_id=%s model=%s duration_s=%.1f sample_rate_hz=%.3f",
        video_id, backend.model_id, duration_s, sample_rate_hz,
    )
    activations = backend.infer(
        video_id=video_id,
        duration_s=duration_s,
        seed=seed,
        sample_rate_hz=sample_rate_hz,
    )

    if activations.dtype != np.float32:
        raise TypeError(f"backend must return float32, got {activations.dtype}")
    if activations.ndim != 2 or activations.shape[1] != NUM_VERTICES:
        raise ValueError(
            f"backend must return (T, {NUM_VERTICES}); got {activations.shape}"
        )
    if float(activations.min()) < 0.0 or float(activations.max()) > 1.0:
        raise ValueError(
            f"backend output out of [0, 1] range: "
            f"[{float(activations.min())}, {float(activations.max())}]"
        )

    num_timepoints = int(activations.shape[0])
    # Full-resolution time axis (one entry per timepoint). Keyframe snapshots
    # index into this at native resolution; the wire payload carries the
    # downsampled axis below so it stays the same length as `region_means`.
    full_timestamps = np.arange(num_timepoints, dtype=np.float64) / sample_rate_hz

    # Reproducibility envelope — CONTRACTS.md §8.
    preprocessing_params = {**DEFAULT_PREPROCESSING_PARAMS, **(extra_params or {})}
    params_json: dict[str, Any] = {
        "duration_s": float(duration_s),
        "sample_rate_hz": float(sample_rate_hz),
        "num_timepoints": num_timepoints,
        "preprocessing": preprocessing_params,
        "created_at_utc": created_at.isoformat(),
    }
    # Optional protocol extension: backends may expose an `extra_params`
    # property (e.g. TribeBackend records torch + CUDA versions and the
    # resolved weights hash). The runner merges whatever the backend
    # offers under a "backend" key so the schema stays open-ended.
    backend_extra = getattr(backend, "extra_params", None)
    if isinstance(backend_extra, dict) and backend_extra:
        params_json["backend"] = dict(backend_extra)
    params_json["config_hash"] = _config_hash(
        model_id=backend.model_id,
        model_version=backend.model_version,
        seed=seed,
        params=params_json,
    )

    activation_path = activations_dir / f"{run_id}.npz"
    sidecar_path = activations_dir / f"{run_id}.meta.json"
    # `compress=False` is the demo / mock-mode escape hatch — `savez` is
    # ~25× faster than `savez_compressed` on a (T, 20484) fp32 tensor, at
    # the cost of ~2× disk. Production callers (real TRIBE, real user
    # data) leave the default on so on-disk activations stay compact.
    save_fn = np.savez_compressed if compress else np.savez
    save_fn(activation_path, activations=activations)

    sidecar = ActivationSidecar(
        inference_run_id=run_id,
        video_id=video_id,
        num_timepoints=num_timepoints,
        sample_rate_hz=sample_rate_hz,
        model_id=backend.model_id,
        seed=seed,
    )
    sidecar_path.write_text(sidecar.model_dump_json(indent=2))

    metric_dicts = aggregate_region_metrics(
        activations, video_id=video_id, inference_run_id=run_id
    )
    region_metrics = [RegionMetrics(**row) for row in metric_dicts]

    region_means = downsample_region_means(activations, max_timepoints=max_wire_timepoints)
    # Pool the time axis onto the SAME bins as region_means so the wire payload
    # honours len(timestamps) == len(region_means[region]) for every region —
    # the invariant the frontend (apps/web/lib/api-v2.ts) enforces.
    wire_timestamps = downsample_timestamps(full_timestamps, max_timepoints=max_wire_timepoints)
    keyframe_vertices = keyframe_vertex_snapshots(
        activations, num_keyframes=num_keyframes, timestamps=full_timestamps
    )

    activation_payload = ActivationOutput(
        inference_run_id=run_id,
        video_id=video_id,
        num_timepoints=num_timepoints,
        sample_rate_hz=sample_rate_hz,
        timestamps=wire_timestamps,
        keyframe_vertices=keyframe_vertices,
        region_means=region_means,
    )

    inference_run = InferenceRun(
        id=run_id,
        video_id=video_id,
        model_id=backend.model_id,
        model_version=backend.model_version,
        seed=seed,
        params_json=params_json,
        created_at=created_at,
        activation_path=str(activation_path),
        status="complete",
    )

    _log.info(
        "event=infer-complete video_id=%s run_id=%s num_timepoints=%d "
        "wire_timepoints=%d activations=%s",
        video_id, run_id, num_timepoints, len(wire_timestamps), activation_path.name,
    )

    return RunArtifacts(
        inference_run=inference_run,
        region_metrics=region_metrics,
        activation_payload=activation_payload,
        activation_path=activation_path,
        sidecar_path=sidecar_path,
    )
