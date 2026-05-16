"""Inference orchestration — STUB.

Owner: ml-inference worker. See docs/worker-briefs/ml-inference.md.

Public entry point used by the API service:

    run_inference(
        *,
        video_id: str,
        duration_s: float,
        seed: int,
        sample_rate_hz: float,
        activations_dir: Path,
        backend: InferenceBackend | None = None,
        extra_params: dict | None = None,
    ) -> RunArtifacts

`RunArtifacts` must carry:
  - inference_run        (matches `shared.schemas.InferenceRun`)
  - region_metrics       (list[RegionMetrics])
  - activation_payload   (matches `shared.schemas.ActivationOutput`)
  - activation_path      (Path on disk to raw NPZ/Parquet)
  - sidecar_path         (Path to `{run_id}.meta.json`)

The runner is responsible for honouring the reproducibility envelope
(CONTRACTS.md §8): model id + version, seed, params, config hash, UTC
timestamp.
"""

from __future__ import annotations

# TODO(ml-inference): implement run_inference + RunArtifacts.
