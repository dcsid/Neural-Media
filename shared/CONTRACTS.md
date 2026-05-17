# Neural Media — Shared Contracts

This file is the **single source of truth** for data shapes that cross service
boundaries. Any change here is a coordinated change across ml-inference,
data-pipeline, api-orchestrator, frontend-dashboard, and brain-viz workers.

The contracts live in three places that **must stay in sync**:

| File                          | Audience                  |
|-------------------------------|---------------------------|
| `shared/CONTRACTS.md`         | Humans (this file)        |
| `shared/schemas.py`           | FastAPI + inference (Pydantic) |
| `shared/types.ts`             | Next.js frontend          |

If you modify any of these, open a PR that touches **all three**.

---

## 1. VideoMetadata

Identifier and descriptive metadata for a single video pulled from a TikTok
export. Populated by the data-pipeline worker.

| Field         | Type            | Notes                                     |
|---------------|-----------------|-------------------------------------------|
| `id`          | string (UUID)   | Stable hash of `source_url`               |
| `source_url`  | string          | Original TikTok URL                       |
| `title`       | string \| null  | Best-effort scrape; may be missing        |
| `author`      | string \| null  | @handle without leading `@`               |
| `duration_s`  | float           | Seconds; `0` if unknown                   |
| `downloaded`  | boolean         | Whether the local file is on disk         |
| `local_path`  | string \| null  | Relative path under `data/videos/`        |
| `tags`        | string[]        | Lowercase; may be empty                   |

## 2. WatchEvent

A single playback occurrence in the user's history.

| Field           | Type           | Notes                                                  |
|-----------------|----------------|--------------------------------------------------------|
| `id`            | string (UUID)  |                                                        |
| `video_id`      | string         | Foreign key → `VideoMetadata.id`                        |
| `watched_at`    | string (ISO-8601 UTC) | Timezone-aware                                  |
| `duration_watched_s` | float \| null | If exposed by export; otherwise null            |
| `completion_pct`| float \| null  | `0..1`                                                |
| `source`       | "tiktok_export" | Free-form provenance string                          |

## 3. InferenceRun

One TRIBE inference invocation against one video.

| Field              | Type           | Notes                                            |
|--------------------|----------------|--------------------------------------------------|
| `id`               | string (UUID)  |                                                  |
| `video_id`         | string         | FK → `VideoMetadata.id`                          |
| `model_id`         | string         | e.g. `tribe-v2` or `tribe-v2-mock`               |
| `model_version`    | string         | Semver-ish; mock uses `0.0.0-mock`               |
| `seed`             | integer        | RNG seed; required for reproducibility           |
| `params_json`      | object         | Free-form preprocessing/inference params         |
| `created_at`       | string (ISO-8601 UTC) |                                           |
| `activation_path`  | string         | Relative path to the activation Parquet/NPZ file |
| `status`           | "pending" \| "running" \| "complete" \| "failed" |                            |

## 4. ActivationOutput (on-disk format)

The raw TRIBE output per video. Stored on disk, **never** sent over the API
in full — only summarized metrics are sent. Format:

- **File**: `data/activations/{inference_run_id}.parquet`
- **Schema**:
  - `t` (float32): seconds from video start, one row per timepoint
  - `v_0000` ... `v_20483` (float32): predicted BOLD per cortical vertex

For the mock inference service, equivalent JSON files live under
`data/sample/mock_inference/` for ease of inspection — the production
contract is Parquet.

A companion sidecar `{inference_run_id}.meta.json` records:

```json
{
  "inference_run_id": "...",
  "video_id": "...",
  "num_vertices": 20484,
  "num_timepoints": 180,
  "sample_rate_hz": 1.5,
  "model_id": "tribe-v2-mock",
  "seed": 7
}
```

## 5. RegionMetrics

Per-region aggregation of an `ActivationOutput`. This is the primary thing the
API serves and the frontend renders. Computed by the aggregator (lives in
`services/inference/neural_media_inference/aggregate.py`).

The canonical region set:

| `region_id`   | Description                          |
|---------------|--------------------------------------|
| `v1`          | Primary visual cortex                |
| `v2`          | Secondary visual cortex              |
| `v3`          | Tertiary visual cortex               |
| `v4`          | V4 (color/form)                      |
| `auditory`    | Primary + belt auditory cortex       |
| `language`    | Lateral language network             |
| `ffa`         | Fusiform face area                   |
| `vwfa`        | Visual word form area                |

`RegionMetrics` JSON shape, per (video, region):

```json
{
  "region_id": "v1",
  "video_id": "...",
  "inference_run_id": "...",
  "mean": 0.42,
  "peak": 0.91,
  "sustained": 0.31,
  "timeseries": [0.10, 0.13, 0.18, ...]
}
```

`timeseries` is the per-region mean activation at each timepoint (so its
length equals `num_timepoints` in the sidecar).

`REGION_VERTEX_MASKS` (the vertex-index sets backing the regions above)
are **disjoint** per-region index sets — no vertex appears in two
regions. Their union MAY be a strict subset of `[0, NUM_VERTICES)`:
vertices outside the eight curated regions (medial wall; motor,
parietal, and prefrontal-control parcels not on the canonical list)
are intentionally unassigned. Implementations using a real cortical
atlas (HCP-MMP1 / Glasser, Destrieux, Yeo7/17, etc.) will report
coverage well below 100% — this is correct anatomy, not a contract
violation. The brain-viz worker handles unassigned vertices via a
sentinel value (`255`) in `fsaverage5.regions.bin`.

## 6. AggregateReport

User-level rollup across the full history. Computed on demand.

```json
{
  "total_videos": 42,
  "total_watch_time_s": 9821.4,
  "first_watched_at": "...",
  "last_watched_at": "...",
  "by_region": {
    "v1": { "mean": 0.38, "peak": 0.94 },
    "auditory": { "mean": 0.22, "peak": 0.81 },
    "...": "..."
  },
  "by_hour_of_day": [ /* 24 floats, mean engagement */ ],
  "by_day_of_week": [ /*  7 floats, mean engagement */ ],
  "by_author": [
    {
      "author": "chefcorpus",
      "videos": 12,
      "total_watch_time_s": 420.5,
      "mean_activation": 0.42,
      "top_region": "ffa"
    }
  ],
  "clusters": [
    { "cluster_id": 0, "size": 12, "exemplar_video_ids": ["..."] }
  ]
}
```

`by_author` is **capped at the top 20** authors and sorted by
`videos` desc, then `total_watch_time_s` desc, then `author` asc.
`author` is the TikTok handle without the leading `@`, or `null`
when the URL didn't carry one (e.g. `tiktokv.com/share/video/<id>/`
share-shortlinks). `videos` counts distinct videos, not impressions
— rewatches collapse into one row. `top_region` is the region with
the highest per-author **peak** across that author's videos
(comparative claim, per `docs/scientific-framing.md`).

May be empty (`[]`) until the aggregator implementation ships; the
frontend's `AuthorPlaceholder` component handles the transitional
state. See `docs/worker-briefs/aggregate-by-author-proposal.md`.

## 7. API Endpoints

All endpoints are namespaced under `/api/v1`. All responses are JSON.
The API is local-only — no auth, but it MUST bind only to `127.0.0.1` by
default and reject `Host` headers other than `localhost`/`127.0.0.1`.

| Method | Path                              | Returns                          |
|--------|-----------------------------------|----------------------------------|
| GET    | `/api/v1/health`                  | `{ "status": "ok" }`             |
| GET    | `/api/v1/videos`                  | `VideoMetadata[]`                |
| GET    | `/api/v1/videos/{video_id}`       | `VideoMetadata`                  |
| GET    | `/api/v1/videos/{video_id}/metrics` | `RegionMetrics[]` (one per region) |
| GET    | `/api/v1/videos/{video_id}/activation` | `ActivationOutput` (downsampled JSON) |
| GET    | `/api/v1/regions`                 | List of canonical region defs    |
| GET    | `/api/v1/aggregate`               | `AggregateReport`                |
| GET    | `/api/v1/watch-events`            | `WatchEvent[]`                   |
| GET    | `/api/v1/inference-runs`          | `InferenceRun[]`                 |
| POST   | `/api/v1/import`                  | `ImportJob` (see §8)             |
| GET    | `/api/v1/import/{job_id}`         | `ImportJob`                      |
| POST   | `/api/v1/import/{job_id}/retry`   | `ImportJob` (new, re-drives `run_pending`) |
| GET    | `/api/v1/capabilities`            | `Capabilities` (see §10)         |

The frontend MUST use only these endpoints. The frontend MUST NOT read files
from the filesystem directly. If you find yourself wanting to, add a new
endpoint here first.

CORS: `GET` is the default. `/api/v1/import*` are the only non-GET surfaces
and are the only reason api-orchestrator's `allow_methods` includes `POST`.

## 8. ImportJob

`POST /api/v1/import` accepts a single file in `multipart/form-data` under
the field name `file`. The file is one of:

- A raw TikTok `user_data.json` (older exports), **or**
- A TikTok `Watch History.txt` (newer exports — flat `Date:`/`Link:`
  blocks; auto-detected by the `.txt` extension), **or**
- A TikTok export `.zip` archive — the orchestrator reads `user_data.json`
  in memory from the archive at any depth.

Optional form field `mode` (default `"mock"`):

- `mock` — orchestrator runs with `MockBackend`, skips yt-dlp + ffmpeg.
  Demo path. Completes in seconds, no GPU. **Predictions are synthetic,
  deterministic from `SHA-256(video_id, seed)` — the video is never
  downloaded or read.** See `docs/scientific-framing.md`.
- `real` — full pipeline: yt-dlp → ffmpeg → `TribeBackend`. Requires
  `pip install '.[real]'` + a GPU + license acceptance on HuggingFace.
  The endpoint returns 400 with structured `error_code` (see below) if
  any of these prerequisites are missing. The frontend should consult
  `/api/v1/capabilities` (§10) to pre-empt this.

Optional date-window form fields cap the events the importer pulls out
of the export before yt-dlp ever runs. Use these to keep the inference
workload tractable on long histories:

- `since` (ISO-8601 UTC, e.g. `2026-05-17T03:11:00Z`) — drop events
  strictly before this timestamp.
- `until` (ISO-8601 UTC) — drop events at or after this timestamp
  (half-open upper bound).
- `days` (int) — convenience for "last N days." Equivalent to setting
  `since = now - N days`. Wins over `since` when both are sent. `0` or
  negative disables the window.

Malformed `since`/`until` returns 400 with `error_code=since_unparseable`.

## 8.1. Error envelope (POST surfaces)

Errors from POST endpoints include a machine-readable `error_code`
alongside the human-readable `detail`:

```json
{
  "detail": "real mode requires the [real] inference extra: pip install 'neural-media-inference[real]'",
  "error_code": "real_extra_missing"
}
```

Frontend uses `error_code` for branching UI; `detail` is for display.
Defined codes:

| `error_code`              | Status | Meaning                                       |
|---------------------------|--------|-----------------------------------------------|
| `file_extension_rejected` | 400    | Uploaded file is not `.json`/`.zip`/`.txt`    |
| `since_unparseable`       | 400    | `since`/`until` not ISO-8601 / `YYYY-MM-DD`  |
| `real_extra_missing`      | 400    | `[real]` Python extra not installed           |
| `real_no_gpu`             | 400    | No CUDA device available                      |
| `real_no_ffmpeg`          | 400    | ffmpeg not on PATH                            |
| `real_no_yt_dlp`          | 400    | yt-dlp not on PATH                            |
| `job_not_found`           | 404    | Retry against unknown job id                  |
| `job_not_retryable`       | 409    | Retry against a `complete` job (only `failed`/`partial` qualify) |

GET endpoints continue to use the FastAPI default `{"detail": "..."}`
envelope; new codes only attach to POST routes where the frontend
needs to branch.

The endpoint returns 200 immediately with an `ImportJob` whose status is
`queued`. Frontend polls `GET /api/v1/import/{job_id}` until status flips
to a terminal value. The orchestrator runs in a background thread on the
API process — single-user, no distributed queue.

If a job is already in flight, POST returns **409** with the running
job's `ImportJob` as the body (not an error envelope — the literal
`ImportJob` shape, so the frontend can pick up the id and resume
polling).

`ImportJob` JSON shape:

```json
{
  "id": "5d0a…",
  "status": "running",
  "mode": "mock",
  "created_at": "2026-05-16T18:01:22Z",
  "updated_at": "2026-05-16T18:01:24Z",
  "completed_at": null,
  "progress": {
    "current": 12,
    "total": 200,
    "phase": "inferring"
  },
  "error": null,
  "source_filename": "user_data.json"
}
```

`ImportJob.status` is one of:

| Status     | Meaning                                                       |
|------------|---------------------------------------------------------------|
| `queued`   | accepted, background thread hasn't started yet                |
| `running`  | at least one `ProgressEvent` received; pipeline in progress   |
| `complete` | orchestrator returned; `failed_count == 0`                    |
| `partial`  | orchestrator returned; both `completed > 0` and `failed > 0`  |
| `failed`   | orchestrator raised before completing (e.g. unparseable export) |

Frontend treats `complete` and `partial` as terminal-success (redirect
to `/`); `failed` is terminal-error.

`ImportJob.progress.phase` is informational, **not** a status value.
Vocabulary mirrors `neural_media_pipeline.orchestrate.Phase`:
`"parsing" | "downloading" | "preprocessing" | "inferring"`, or
`null` until the first progress event.

`progress.total` is `null` until the parsing phase completes (the
orchestrator doesn't know the video count yet).

## 9. Reproducibility envelope

Every `InferenceRun` MUST log:

- model id + version
- random seed
- video preprocessing params (resolution, fps, audio sample rate)
- TRIBE configuration hash
- wallclock timestamp (UTC)

This is non-negotiable — it is the difference between "ran a model" and "did
science with a model." The mock inference service must also obey this so the
contract is exercised end-to-end before TRIBE arrives.

## 10. Capabilities

`GET /api/v1/capabilities` reports which modes will actually run on the
current host. Frontend consults this on `/import` mount to disable the
Real toggle (with a tooltip explaining why) before the user wastes a
submission.

```json
{
  "mock": true,
  "real": false,
  "real_blockers": ["missing-extra", "missing-ffmpeg", "missing-yt-dlp"]
}
```

`mock` is currently always `true` — the `MockBackend` ships in the base
install and has no external dependencies.

`real` is `true` iff `real_blockers` is empty. Each blocker is a short
token from `{missing-extra, missing-ffmpeg, missing-yt-dlp,
missing-gpu}`, listed in priority order (most-fundamental first). The
priority order also governs which token becomes the `error_code` on a
real-mode POST when prerequisites are missing.

| `real_blockers` token | Remediation                                          |
|-----------------------|------------------------------------------------------|
| `missing-extra`       | `pip install -e 'services/inference[real]'`          |
| `missing-ffmpeg`      | `brew install ffmpeg` (or distro equivalent)         |
| `missing-yt-dlp`      | `pip install yt-dlp` (pulled by `[real]` extra)      |
| `missing-gpu`         | Use a CUDA host; CPU is technically possible but glacial |

Mirrors `Capabilities` in `shared/types.ts` and `shared.schemas.Capabilities`.

## 11. Retry semantics

`POST /api/v1/import/{job_id}/retry` re-drives the orchestrator's
`run_pending()` against a previously-submitted import job. The original
file does not need to be re-uploaded — `pipeline_jobs` rows survive
across runs.

Allowed: `status in {failed, partial}`. Anything else returns 409 with
`error_code=job_not_retryable`. Unknown ids return 404 with
`error_code=job_not_found`.

Same singleton-gate semantics as the original POST: if another job is
in flight, returns 409 with the in-flight `ImportJob` as the literal
body (not an error envelope), so the frontend can pick up the id and
resume polling.

On success, returns 200 with a **new** `ImportJob` (new `id`) whose
`status` starts at `queued`. The old job's row stays where it was so
the history isn't lost.
