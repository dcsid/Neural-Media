# Mock-mode ingest performance — round 2

The mock-mode ingest path drives the demo: parse export →
synthesize MockBackend activations → bulk SQLite writes. Real-mode is
rate-limited by TikTok (~2 s/video network), which we explicitly do
NOT try to optimise.

Reproduce with `make bench-ingest`. The wall times below are best of
2–3 runs on a 14-core M-series Mac, Python 3.14.

## Targets vs measured

| Workload                  | Round 1 baseline | Round 2 (this branch) | Target | Status |
| ------------------------- | ---------------: | --------------------: | -----: | :----: |
| 744 videos (sample)       |          ~10 s   |                **0.50 s** |   <3 s | ✅ 20× |
| 1 000 videos (synthetic)  |          ~13.7 s |                **0.64 s** |      — | ✅ 21× |
| 5 000 videos (synthetic)  |        ~70 s     |                **2.97 s** |  <15 s | ✅ 24× |
| 10 000 videos (synthetic) |       ~137 s     |                **6.01 s** |      — | ✅ 23× |
| 100 000 videos (synth)    |         22 min   |                **71 s**   |  <4 min | ✅ 18× |
| 129 616 videos (real user export) |       — |                **99 s**   |      — | — |

All runs use `--mock --purge-after-inference --purge-activations` (the
demo path the dashboard runs against).

## What changed in round 2

1. **SQLite batching.** `_upsert_videos`, `_upsert_watch_events`,
   `_enqueue_pending`, `_persist_run` and the new mock-fast bulk
   variants all use `executemany()` inside a single transaction
   instead of N per-row `INSERT`s. Cuts the
   `sqlite3.Connection.__exit__` cost that showed up at 0.57 s on the
   round-1 profile.
2. **Mock-fast driver (`_drive_mock_fast`).** When `skip_download +
   skip_preprocess` are both on AND the configured `inference_fn` is
   the module-default `run_inference` (i.e. production CLI, not tests
   that inject closures), the orchestrator takes a separate fast path:
   pre-resolve every video's args up-front, parallelise inference,
   bulk-write everything at the end. Bypasses the per-video
   `_drive_one` round-trips through SQLite.
3. **Parallel ProcessPool.** Mock-fast uses
   `ProcessPoolExecutor(mp_context=fork)`. `fork` beats `spawn` on
   macOS / Linux because workers inherit the parent's already-imported
   numpy + pydantic instead of re-importing. Worker count is
   `min(cpu_count, max(1, total // 50))` so small ingests stay
   sequential (start-up tax) and large ingests fan out across every
   core. Configurable via `OrchestratorConfig.parallel_workers`.
4. **Lean worker for `purge_activations`** (`_mock_worker_run_lean`).
   The full `run_inference` would write `(T, 20484)` fp32 npz + JSON
   sidecar that the orchestrator then *unlinks immediately* under
   `purge_activations=True`. Under 14 parallel workers, those writes
   bottleneck on the activations directory and turn a CPU problem
   into a disk problem — the lean worker skips the disk writes
   entirely (still calls `MockBackend.infer` + `aggregate_region_metrics`
   + builds the reproducibility envelope, so the SQLite rows that
   land are byte-equal). This is the single biggest win: 1 000 videos
   went from 11.85 s → 0.64 s when we stopped writing 1 000 npzs
   we were about to delete.
5. **Coalesced progress events.** Per-video phase events are emitted
   every `max(1, total // 200)` videos so the dashboard sees ~200
   ticks regardless of batch size. Small fixtures (N ≤ 200) get
   every-video resolution; large ingests get one tick per 0.5 %.
6. **Deferred payload JSON.** The orchestrator-side
   `<run_id>.json` sidecar (downsampled `ActivationOutput`, mainly
   `keyframe_vertices`) is no longer written when
   `purge_activations=True` — it would be unlinked moments later.
   Was ~30 % of the round-1 cProfile.

## Profile (before round 2)

```
1000 videos, mock + purge, sequential: 13.65 s
  pydantic to_json (sidecar + payload): 4.21 s
  MockBackend.infer (numpy CPU):        3.50 s
  numpy.savez (npz writes):             0.79 s
  sqlite3 Connection __exit__:          0.57 s
  numpy.tolist (region timeseries):     0.73 s
  pydantic validate_python:             0.55 s
```

## Profile (after round 2, same workload)

```
1000 videos, mock + purge, 14 workers fork: 0.64 s
  parent: pool setup, bulk SQLite, args build: ~0.1 s
  workers (14 parallel × ~50 ms): MockBackend.infer + aggregate
  no disk writes, no pydantic serialisation
```

## Test seam preservation

- All 105 pipeline tests still pass — the fast path is opt-in via
  `inference_fn is run_inference`. Every test injects a closure-based
  fake `inference_fn`, which fails the identity check and falls
  through to the sequential `_drive_all` path. Production CLI runs
  pass the default and get the fast path.
- The CLI surface (`python -m neural_media_pipeline --mock --days N
  --purge-after-inference --purge-activations`) is unchanged.
- SQLite contract preserved: same rows, same indices, same
  idempotence (INSERT OR IGNORE / INSERT OR REPLACE).

## Things that wouldn't help

- **`compress=False`.** Already honoured via the existing sniff
  mechanism in `_resolve_compress_flag`; would help further but the
  runner upstream doesn't currently expose the kwarg, and the lean
  worker skips the write entirely anyway.
- **Smaller worker counts.** `min(cpu_count, total // 50)` already
  reduces workers on small ingests. Forcing fewer workers on large
  ingests slows them down (each worker still has to handle 1k+
  videos serially).
- **`spawn` start method.** 2× slower than `fork` on macOS because
  each worker re-imports numpy + pydantic from cold.

## How to re-run

```
make bench-ingest                          # real Watch History.txt
make bench-ingest N=5000                   # synthetic
make bench-ingest EXPORT=/some/other.txt   # custom export
```
