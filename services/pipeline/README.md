# neural-media-pipeline

TikTok export → normalized videos + watch events + TRIBE inference, persisted
to a SQLite catalog. **Local-first: nothing leaves the machine.** This is the
ingest engine behind Neural Media — it turns a raw "Download your data" export
into the rows the API serves and the dashboard renders.

The same SQLite file written here is read by the api worker's `SqliteStore`,
so the schema in [`orchestrate.py`](neural_media_pipeline/orchestrate.py) is a
coordination surface — see [`shared/CONTRACTS.md`](../../shared/CONTRACTS.md).

## The pipeline at a glance

```
                                 ┌──────────────────────────────────────────┐
 user_data.json / .zip / .txt    │              Orchestrator                 │
            │                     │  (SQLite-backed job queue, resumable)     │
            ▼                     │                                            │
   ┌─────────────────┐           │   for each pending video:                 │
   │   importer.py   │  videos   │     queued → downloaded → preprocessed →  │
   │  parse_export() │──────────▶│              complete  (or failed)        │
   └─────────────────┘  +events  │       │           │            │          │
                                 │       ▼           ▼            ▼          │
                                 │ downloader.py  preprocess.py  run_inference│
                                 │  (yt-dlp,       (ffmpeg →      (TRIBE /     │
                                 │   retry+UA)     TRIBE shape)   MockBackend) │
                                 └──────────────────────────────────────────┘
                                                     │
                                                     ▼
                                         data/sqlite/neural_media.db
                                  (videos, watch_events, inference_runs,
                                   region_metrics, pipeline_jobs)
```

| Module | Responsibility |
|--------|----------------|
| [`importer.py`](neural_media_pipeline/importer.py)     | Parse a TikTok export (`user_data.json`, a `.zip` containing it, or the newer `Watch History.txt`) into `VideoMetadata` + `WatchEvent` rows. Deterministic UUID5 ids; optional `since`/`until` date window. |
| [`downloader.py`](neural_media_pipeline/downloader.py) | One yt-dlp seam with full-jitter exponential backoff, User-Agent rotation, and an on-disk dedup cache. Never hits the network on a cache hit. |
| [`preprocess.py`](neural_media_pipeline/preprocess.py) | ffmpeg normalization to TRIBE v2's input shape. The target resolution/fps/audio-rate are imported from `neural_media_inference` so they cannot drift. |
| [`orchestrate.py`](neural_media_pipeline/orchestrate.py) | The SQLite-backed state machine that drives each video download → preprocess → infer, persists rows, and is idempotent/resumable. Hosts the mock-mode fast path. |
| [`cli.py`](neural_media_pipeline/cli.py)               | `python -m neural_media_pipeline` entry point. |

## Usage

```bash
# Full pipeline (needs yt-dlp + ffmpeg): download, preprocess, run inference.
python -m neural_media_pipeline path/to/user_data.json

# Demo / mock mode — no yt-dlp, no ffmpeg, no GPU. Synthetic deterministic
# predictions; completes in seconds. This is the path the dashboard runs.
python -m neural_media_pipeline export.zip --mock

# Parse only and print counts; touches no DB and downloads nothing.
python -m neural_media_pipeline export.zip --dry-run

# Drive previously-queued/failed jobs without re-importing the export.
python -m neural_media_pipeline --run-pending

# Cap the workload to a recent window (finest unit wins).
python -m neural_media_pipeline export.txt --mock --days 30
```

Useful flags (`--help` for the full list):

| Flag | Effect |
|------|--------|
| `--data-root DIR` | Override `--db`/`--videos-dir`/`--processed-dir`/`--activations-dir` to subdirs of `DIR` at once. |
| `--mock` | Shortcut for `--skip-download --skip-preprocess`. Demo path. |
| `--dry-run` | Parse + print counts only. Requires an export. |
| `--run-pending` | Skip parsing; drive existing queued/failed jobs. |
| `--since/--until/--days/--hours/--minutes` | Restrict events to a half-open `[since, until)` window. |
| `--purge-after-inference` / `--purge-activations` | Tiered disk cleanup once `RegionMetrics` are durable in SQLite. |
| `-v` / `-vv` | INFO / DEBUG logging (default is WARNING). |

Exit code is `0` only on a clean run; **any** failure (including a *partial*
run where some videos completed and some failed) exits `1`.

### Programmatic

```python
from neural_media_pipeline.orchestrate import Orchestrator, OrchestratorConfig

cfg = OrchestratorConfig(data_root="data", skip_download=True, skip_preprocess=True)
with Orchestrator(cfg) as orch:
    summary = orch.run("export.zip", progress=my_callback)  # progress is optional
print(summary.completed, summary.failed)
```

`Orchestrator.run()` is what the api worker calls from its background import
thread; `progress=` receives a `ProgressEvent` at each phase boundary.

## Mock mode & the fast path

With both skip flags set (`--mock`) and the default `run_inference`, the
orchestrator takes a parallel, bulk-write fast path (`_drive_mock_fast`) that
fans inference out across a `ProcessPoolExecutor` and batches all SQLite
writes. Predictions are synthetic and deterministic from `SHA-256(video_id,
seed)` — the video is never downloaded or read. See
[`docs/bench-results.md`](../../docs/bench-results.md) for the profile (1000
videos: ~12 s → ~0.6 s) and the lean-vs-full worker distinction.

## Error handling

The pipeline is built to degrade rather than crash on the messy inputs a real
export throws at it:

- **Tolerant importer.** Unknown fields are ignored and entries missing the
  required `Date`/`Link` are dropped with a debug log — TikTok drifts the
  export shape between releases, so the parser skips bad *entries* instead of
  failing the whole import.
- **Download retries.** Per-video failures retry with full-jitter backoff up
  to `max_attempts`; only an exhausted budget raises `DownloadError`. In batch
  mode a single video's failure is captured as a row, not a hard stop.
- **Resumable jobs.** State lives in `pipeline_jobs`; a crash mid-run resumes
  where it left off (downloads/preprocessing are idempotent on disk, and a
  zero-byte cached file is re-fetched rather than trusted).
- **Corrupt catalog.** Opening a `db_path` that isn't a valid SQLite file
  raises a clear *"corrupted or not a database; delete it and re-run"* error
  instead of a cryptic `sqlite3` message.
- **Duration probe.** If `ffprobe` is missing or fails, the run falls back to
  a synthetic/default duration (logged at DEBUG) rather than failing.

Privacy: no module ever logs a full source URL — the stable id (a hash of the
URL) is the only handle that appears in logs. State-changing and
probe-failure log lines follow the `event=<name> key=value` convention from
`CONTRACTS.md §12`.

## Testing

```bash
cd services/pipeline
../../.venv-dev/bin/python -m pytest -q     # PYTHONPATH is set by `make test`
```

Tests never touch the network, ffmpeg, ffprobe, or a GPU — every external side
effect is an injectable callable that the suite replaces with a deterministic
fake. New behavior should land with a regression test in
[`tests/`](tests/), not in the repo-root `tests/`.
