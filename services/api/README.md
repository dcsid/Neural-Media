# neural-media-api

FastAPI orchestrator for **Neural Media** — the local-first, single-user service
that turns a TikTok watch-history export into a browsable catalog of predicted
average BOLD fMRI responses.

> ⚠️ These are *predicted* group-average responses from the TRIBE v2 model
> (averaged over 720 training subjects), **not** a measurement of any
> individual's brain. See
> [`docs/scientific-framing.md`](../../docs/scientific-framing.md).

## What it does

The API is the read + ingest control plane:

- **Reads** the catalog (videos, watch events, per-region metrics, activations,
  and the aggregate "wrapped" report) for the dashboard.
- **Ingests** an uploaded export: stages the file, registers an `ImportJob`, and
  drives `neural_media_pipeline.Orchestrator` on a background thread.

It owns no data of its own — the data-pipeline worker writes the SQLite catalog;
the API only reads it (plus the `import_jobs` table it manages). Wire contracts
for every route and payload live in
[`shared/CONTRACTS.md`](../../shared/CONTRACTS.md) (§7 routes, §8 import jobs).

## Entry point: `create_app`

```python
neural_media_api.main.create_app(store=None, *, import_runner=None) -> FastAPI
```

Both arguments are injectable so tests can pass a fixture `Store` and a stub
`ImportRunner` — no SQLite, yt-dlp, ffmpeg, or TRIBE weights required. With no
arguments it selects a backing store from the environment
(`NEURAL_MEDIA_DB_PATH` → `SqliteStore`, otherwise the committed sample
fixtures) and wires a real `ImportRunner`. The module also exposes a ready
`app = create_app()` for `uvicorn`.

## Key designs

### Single-job gate

This is a single-user app, so at most one import runs at a time. `ImportRunner`
guards a `threading.Lock` plus an in-memory "currently running job id"; a second
POST while a job is in flight raises `JobAlreadyRunning`, which the route turns
into a `409` carrying the in-flight job as the literal `ImportJob` body (so the
frontend can resume polling it). The claim → check → create step is fully
serialized under the lock — see the comment on `ImportRunner._claim_slot`.

### Background-thread model

`POST /api/v1/import` returns immediately with a `queued` job; the orchestrator
runs on a daemon thread. Progress reaches the job row two ways: an `on_progress`
callback when the pipeline supports it, and a sibling poller thread that samples
the pipeline's own `pipeline_jobs` table otherwise. Either signal updates the
`progress.*` counters; the top-level `status` only flips at submission and at a
terminal state. A crash mid-run leaves a recoverable row, and any orphaned
non-terminal rows are swept to `failed` on the next startup.

### Loopback-only security

The app binds to `127.0.0.1`, and `LoopbackOnlyMiddleware` rejects any request
whose `Host` header is missing or not `localhost` / `127.0.0.1` —
defense-in-depth against DNS rebinding from a browser on the same machine. CORS
is opened only to the local dev frontend on port 3000.

## Layout

| Module | Responsibility |
| --- | --- |
| `main.py` | `create_app`, routes, middleware, structured error envelope |
| `import_jobs.py` | `ImportRunner` gate + background worker, capability probing |
| `sqlite_store.py` | read-only `SqliteStore` + `init_db` schema |
| `store.py` | `Store` protocol + `SampleStore` (committed fixtures) |
| `aggregate.py` | `AggregateReport` rollup (cached by store version) |
| `debug.py` | `/api/v1/debug` observability snapshot |
| `schemas_re_export.py` | single local import surface for `shared.schemas` |

## Running the tests

Tests use a stub orchestrator and temp SQLite DBs, so no external binaries are
needed. The repo hides the editable `*.pth` installs and injects `PYTHONPATH`
instead (see the root `Makefile`), so run via `make` or set `PYTHONPATH`
yourself. From the repo root:

```bash
make test-python            # inference + pipeline + api suites
# …or just this package:
PYTHONPATH="$PWD/services/pipeline:$PWD/services/api:$PWD/services/inference" \
  .venv-dev/bin/python -m pytest services/api -q
```
