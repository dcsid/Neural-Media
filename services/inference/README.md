# services/inference

TRIBE v2 inference for Neural Media: turn a preprocessed video into a
`(T, 20484)` cortical-surface activation tensor, then aggregate it into the
region metrics and wire payload the API serves.

## Backends

Both conform to the `InferenceBackend` protocol (`backend.py`) and return
`float32` activations of shape `(num_timepoints, 20484)` in `[0, 1]`, so the
runner is backend-agnostic.

- **`MockBackend`** (default) — deterministic synthesized activations seeded by
  `(video_id, seed)`. No GPU, no weights, no network. Lets the whole system run
  end-to-end without TRIBE installed.
- **`TribeBackend`** (`backend_tribe.py`, gated by `pip install -e '.[real]'`) —
  the real TRIBE v2 forward pass. Fully implemented: it resolves the HuggingFace
  commit sha for the reproducibility envelope, runs the documented
  `get_events_dataframe` → `predict` path, takes the leading 20,484 cortical
  vertices, and squashes the z-scored output through a logistic sigmoid into
  `[0, 1]`. torch/tribev2 are imported lazily, so importing this package on a
  mock-only install never pulls the multi-GB deps. Real mode needs a GPU host —
  see `docs/worker-briefs/ml-inference-status.md` and `docs/real-mode-setup.md`.

## What `run_inference` emits

Per run, written next to each other under `activations_dir`:

1. `{run_id}.npz` — the raw `(T, 20484)` fp32 tensor (`np.savez_compressed`;
   `compress=False` switches to the faster uncompressed `savez` for demos).
2. `{run_id}.meta.json` — the `ActivationSidecar` from `shared/schemas.py`.

…and returned in-memory as a `RunArtifacts`:

- `region_metrics` — one `RegionMetrics` row per canonical region.
- `activation_payload` — the wire-format `ActivationOutput`: a **downsampled**
  `region_means` series per region, a matching-length `timestamps` axis, and
  full-width vertex snapshots at a few `keyframe_vertices`. Frontends never pull
  the raw 20k arrays. `len(timestamps) == len(region_means[region])` holds for
  every region — the invariant `apps/web/lib/api-v2.ts` enforces.
- `inference_run` — the `InferenceRun` row with the full reproducibility
  envelope.

## Aggregate layer (`aggregate.py`)

- `REGION_VERTEX_MASKS` — per-region vertex sets from the HCP-MMP1 atlas
  (Glasser et al. 2016) projected to fsaverage5. Disjoint, and deliberately do
  **not** tile the cortex. Committed at `data/region_masks.json`; regenerate via
  `scripts/build_region_masks.py` (the `.[atlas-build]` extra).
- `aggregate_region_metrics` — full-resolution per-region `mean` / `peak` /
  `sustained` (75th percentile) plus the timeseries.
- `downsample_region_means` / `downsample_timestamps` — mean-pool the region
  series and the time axis onto the **same** bins for the wire payload, so the
  two never disagree on length.
- `keyframe_vertex_snapshots` — full 20,484-dim vectors at evenly-spaced frames.

## Reproducibility envelope

Every `InferenceRun.params_json` carries model id + version, seed, preprocessing
params, a config hash, and a UTC timestamp (CONTRACTS.md §8). `TribeBackend` adds
the resolved weights sha, torch/CUDA versions, and the output transform. Enforced
by `runner.py`.

## Scripts

Package scripts (`services/inference/scripts/`):

- `build_region_masks.py` — regenerate `data/region_masks.json` from the atlas.
- `validate_real_mode.py` — pre-flight check that TRIBE and its transitive
  HuggingFace repos are reachable before attempting a real run.

Repo-root scripts (`scripts/`, offline counterparts of the cloud path):

- `predict_one_url.py` — one video URL → activation JSON in the HF Space callback
  shape (`--mock` for a torch-free run).
- `build_demo_gallery.py` — precompute the `/single/gallery` demo payloads.

## Tests

```
cd services/inference && ../../.venv-dev/bin/python -m pytest -q
```

Real-mode smoke tests are marked `integration` and skipped by default; run them
on a GPU host with the `[real]` extra installed:

```
NEURAL_MEDIA_TRIBE_FIXTURE=/path/to/clip.mp4 pytest -m integration
```
