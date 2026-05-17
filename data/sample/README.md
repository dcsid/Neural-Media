# data/sample

Self-contained sample inputs and pre-generated mock outputs so the vertical
slice runs end-to-end without GPU, without downloading any video, and
without any external dependency beyond NumPy + Pydantic + FastAPI.

```
tiktok_export/user_data.json    Sample TikTok export (older JSON shape) — 8 watched videos
tiktok_export/watch_history.txt Sample TikTok export (newer .txt shape) — same 8 events
videos/                         (intentionally empty; real videos never committed)
mock_inference/                 Pre-generated per-video activation payloads (JSON)
activations/                    Cache dir for raw NPZ outputs; gitignored
```

## Regenerating mock outputs

```
make sample
```

That target invokes `scripts/build_sample_outputs.py`, which reads
`tiktok_export/user_data.json`, runs the MockBackend per video, and writes
fresh JSON activation payloads + region-metric snapshots under
`mock_inference/`. Output is deterministic given a seed.

## A note on the videos directory

The pipeline downloads videos here when run against a real TikTok export.
The directory is committed empty + `.gitignore`d for everything else,
because committing video data is both wasteful and copyright-sensitive.
