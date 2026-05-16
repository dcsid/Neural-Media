# Worker brief — ml-inference

## Mission

Own the TRIBE v2 inference path. Ship a deterministic mock backend that
unblocks the rest of the team on day one, then bring up the real backend.
Own the region aggregation that turns raw vertex activations into the
`RegionMetrics` consumed by the API.

## Owned files / directories

- `services/inference/**` (everything under this directory)

Specifically:

- `services/inference/neural_media_inference/backend.py` — `InferenceBackend`
  protocol, `MockBackend`, eventually `TribeBackend`.
- `services/inference/neural_media_inference/aggregate.py` —
  `REGION_VERTEX_MASKS`, `aggregate_region_metrics`,
  `downsample_region_means`, `keyframe_vertex_snapshots`.
- `services/inference/neural_media_inference/runner.py` — `run_inference`
  + `RunArtifacts` glue.
- `services/inference/tests/**` — unit tests.
- `services/inference/pyproject.toml` (you may add deps; coordinate
  before adding heavyweight ones).

You may also add `services/inference/scripts/` for offline tooling
(e.g. building sample mock outputs from the committed TikTok export).

## Files this worker must NOT touch

- `shared/**` — contracts. Any change is a coordinated cross-team PR.
- `services/api/**` — owned by api-orchestrator.
- `apps/web/**` — owned by frontend-dashboard and brain-viz.
- `data/sample/tiktok_export/**` — owned by data-pipeline.
- Repo-root tooling (`Makefile`, `README.md`, `.gitignore`).

## Deliverables

1. **MockBackend**: deterministic given `(video_id, seed)`. Output shape
   `(num_timepoints, 20484)` fp32 in `[0, 1]`. Implemented without GPU
   or TRIBE installed.
2. **TribeBackend** (gated by `pip install '.[real]'`): real TRIBE v2
   forward pass. Honour the same protocol as MockBackend so swapping
   backends is a one-line change in the runner.
3. **Aggregation**: real region masks (HCP MMP1 / Glasser parcels, or
   whatever atlas TRIBE evaluates against) covering at minimum the
   regions in `shared/schemas.REGION_IDS`. Placeholder masks in the
   scaffold MUST be replaced before any external demo.
4. **Runner**: emits `RunArtifacts` matching CONTRACTS.md §4 + §5, plus
   on-disk `{run_id}.npz` and `{run_id}.meta.json`. Reproducibility
   envelope (CONTRACTS.md §8) is enforced here.
5. **Sample-output build script** (e.g. `scripts/build_sample_outputs.py`)
   so reviewers can regenerate `data/sample/mock_inference/` from the
   committed TikTok export. The api-orchestrator worker depends on this
   output existing for the vertical slice.

## Interfaces this worker must preserve

- `from neural_media_inference import run_inference, MockBackend,
  REGION_VERTEX_MASKS, aggregate_region_metrics` — these names are
  imported by the api-orchestrator.
- The on-disk format of activation NPZ + sidecar (CONTRACTS.md §4).
- The keys in `REGION_VERTEX_MASKS` MUST be a subset of
  `shared.schemas.REGION_IDS` and SHOULD cover all of them.
- The shape of `RegionMetrics` dicts emitted by aggregation MUST match
  `shared.schemas.RegionMetrics`.

## How to test the work

```
cd services/inference
pip install -e '.[dev]'
pytest -q
```

Smoke tests should cover at minimum:

- Output shape is `(T, 20484)` fp32, values in `[0, 1]`.
- Determinism: identical `(video_id, seed)` → byte-identical activations.
- Different `video_id` with same seed produces different activations.
- `aggregate_region_metrics` returns one row per region in
  `REGION_VERTEX_MASKS`, and each row matches the `RegionMetrics`
  contract.
- `run_inference` writes the NPZ + sidecar and the sidecar carries the
  full reproducibility envelope.

## Scientific-framing constraints

- The mock backend must never be presented to a user as "your brain."
  Its only role is to exercise the contract.
- `model_id` strings must be honest: `"tribe-v2"` for the real backend,
  `"tribe-v2-mock"` for the mock. Never label mock outputs as real.
- Comparative claims only — do not return a "user engagement score" from
  this layer.

## Out of scope for this worker

- API routing or HTTP concerns — that is api-orchestrator.
- Video download / preprocessing — that is data-pipeline.
- Any frontend code.
- Persistence to SQLite — emit dicts; api-orchestrator persists them.
