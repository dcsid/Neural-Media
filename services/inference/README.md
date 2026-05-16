# services/inference

TRIBE v2 inference wrapper. Two backends:

- `MockBackend` (default) — deterministic synthesized activations seeded by
  `(video_id, seed)`. Lets the rest of the system run end-to-end without
  GPU access. The output shape and sidecar fields match the real backend.
- `TribeBackend` (gated by `pip install '.[real]'`) — real TRIBE v2 forward
  pass. Owned by the **ml-inference** worker; not present in this scaffold
  beyond a TODO stub.

## What gets emitted per inference run

1. `data/activations/{run_id}.parquet` — `t` + `v_0000..v_20483` rows.
2. `data/activations/{run_id}.meta.json` — `ActivationSidecar` from
   `shared/schemas.py`.
3. A `RegionMetrics` row per (video, region) suitable for direct serving by
   the API.

The mock dev path also writes inspection-friendly JSON under
`data/sample/mock_inference/` so reviewers can eyeball the shape without
needing pyarrow.

## Determinism / reproducibility envelope

Every `InferenceRun` MUST log model id + version, seed, preprocessing params,
config hash, and a UTC timestamp. This is enforced by `runner.py`.
