# Proposal: `GET /api/v1/debug` (CONTRACTS.md §7) + structured logging convention

**From:** integration-tests worker (round 2)
**To:** integration lead → api-orchestrator worker
**Status:** proposal, not yet in `shared/CONTRACTS.md`. Implementation has
already landed under `services/api/neural_media_api/debug.py` with six
tests in `services/api/tests/test_debug.py`; this brief asks for the
contract row to be added so the surface is durable.

## Why

There is no single place to ask the running API "what state are you in?".
During the demo I had to `sqlite3` the catalog, `du -sh` the data
directories, and re-hit `/capabilities` separately to get an answer to
"is this process healthy, and what does it think the world looks like?".
Operationally we have one user and one process, so a single observability
endpoint is sufficient — no Prometheus, no /metrics, no auth.

The endpoint also gives the frontend a cheap way to render a "system
status" sliver in the corner of the dashboard (planned by
frontend-dashboard for the post-demo iteration) without needing to fan
out four GETs.

## Proposed CONTRACTS.md §7 row

Add this row to the table under §7:

| Method | Path                              | Returns                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/api/v1/debug`                   | `DebugReport`                    |

And the following `DebugReport` shape under §7 (or as a new §10):

```json
{
  "version": "0.2.0",
  "db_path": "/abs/path/to/data/sqlite/neural_media.db",
  "videos_dir": "/abs/path/to/data/videos",
  "counts": {
    "videos": 510,
    "watch_events": 510,
    "inference_runs": 510,
    "import_jobs": 6
  },
  "latest_import": { /* ImportJob (§8) | null */ },
  "capabilities": {
    "mock": true,
    "real": false,
    "real_blockers": ["missing-extra", "missing-ffmpeg"]
  },
  "disk_usage": {
    "videos":      0,
    "activations": 3545976712,
    "imports":     45020256,
    "sqlite":      7503872
  },
  "uptime_s": 412.7
}
```

Field semantics:

- `version` — `FastAPI.title.version`. Bumps with every release.
- `db_path` / `videos_dir` — absolute, fully-resolved paths. Useful when
  the user has overridden them via `NEURAL_MEDIA_DB_PATH` /
  `NEURAL_MEDIA_IMPORTS_DIR` and forgotten where the catalog lives.
- `counts` — `SELECT COUNT(*) FROM <table>` for each. Missing tables /
  missing DB degrade to `0` (same contract as `SqliteStore._query`).
- `latest_import` — the most-recent `ImportJob` row regardless of
  status. `null` if no rows yet. Shape is the literal §8 `ImportJob`.
- `capabilities` — identical body to `GET /api/v1/capabilities`. Folded
  in here so a "show me everything" call doesn't need a second request.
- `disk_usage` — recursive sum of `st_size` over each directory, plus
  the size of the SQLite file. Missing directories report `0`. The walk
  is bounded by the local-only single-user data layout — no need for
  pagination.
- `uptime_s` — seconds since the API process booted (monotonic clock,
  sampled at module import).

## Logging conventions added in the same round

I added INFO-level structured logs at three event sites where we
previously had no signal. All use `event=<name> key=value ...` so the
output greps cleanly:

| Site                                                                          | Log shape                                                                                          |
|-------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `ImportRunner.submit()`                                                       | `event=import_submitted op=ingest job_id=… mode=… source_filename=… since=… until=…`              |
| `ImportRunner.submit_retry()`                                                 | `event=import_submitted op=retry  job_id=… prev_job_id=… mode=… source_filename=…`                |
| `POST /api/v1/import` — extension reject                                      | `event=import_rejected reason=file_extension filename=…`                                          |
| `POST /api/v1/import` — real-mode capability reject                           | `event=import_rejected reason=real_mode_blocked blockers=… error_code=…`                          |
| `Orchestrator._purge_video_artifacts*`                                        | `event=cleanup_started kind=videos {count\|video_id}=…`                                            |
| `Orchestrator._purge_activation_artifacts*`                                   | `event=cleanup_started kind=activations {count\|video_id+run_id}=…`                                |

The existing `_log.warning` lines on cleanup failures stay as-is — they
already report what they need to. The convention is: any operation that
mutates user-visible state OR is rejected at a policy boundary should
emit one `event=…` line at INFO. Pre-existing log messages have NOT been
reformatted.

## Out-of-scope (deliberately)

- No `/metrics` Prometheus surface. Local-only, single user — there is
  no scraper.
- No auth on `/debug`. The loopback-only middleware already gates
  everything; `/debug` does not leak more than `/capabilities` +
  `/import` + the directory paths the user themselves configured.
- No per-table size breakdown beyond row counts. `disk_usage.sqlite`
  covers the whole DB file; granular row-size is a `sqlite3 .schema`
  call away when needed.
