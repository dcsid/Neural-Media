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
    keyframe_vertex_snapshots,
)
from .backend import InferenceBackend, MockBackend


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
) -> RunArtifacts:
    backend = backend if backend is not None else MockBackend()
    activations_dir = Path(activations_dir)
    activations_dir.mkdir(parents=True, exist_ok=True)

    # run_id / created_at overrides exist for fixture regeneration only
    # (scripts/build_sample_outputs.py). Production callers leave them None.
    run_id = run_id if run_id is not None else str(uuid.uuid4())
    created_at = created_at if created_at is not None else datetime.now(timezone.utc)

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
    timestamps = (np.arange(num_timepoints, dtype=np.float64) / sample_rate_hz).tolist()

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
    np.savez_compressed(activation_path, activations=activations)

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
    keyframe_vertices = keyframe_vertex_snapshots(
        activations, num_keyframes=num_keyframes, timestamps=timestamps
    )

    activation_payload = ActivationOutput(
        inference_run_id=run_id,
        video_id=video_id,
        num_timepoints=num_timepoints,
        sample_rate_hz=sample_rate_hz,
        timestamps=timestamps,
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

    return RunArtifacts(
        inference_run=inference_run,
        region_metrics=region_metrics,
        activation_payload=activation_payload,
        activation_path=activation_path,
        sidecar_path=sidecar_path,
    )
