# Neural Media — Shared Contracts

This file is the **single source of truth** for data shapes that cross service
boundaries. Any change here is a coordinated change across ml-inference,
data-pipeline, api-orchestrator, frontend-dashboard, and brain-viz workers.

The contracts live in three places that **must stay in sync**:

| File                          | Audience                  |
|-------------------------------|---------------------------|
| `shared/CONTRACTS.md`         | Humans (this file)        |
| `shared/schemas.py`           | Inference + Lambdas (Pydantic) |
| `shared/types.ts`             | Next.js frontend          |

If you modify any of these, open a PR that touches **all three**.

> **Reading guide.** Sections 1 and 3–5 (plus 9) define the shared **data
> model**; **§13 is the canonical API contract** for the v2 single-video
> product. The v1 local-dashboard sections (watch-history import, aggregates,
> capabilities, debug) were removed with that product — the gaps in the
> numbering below are intentional.

---

## 1. VideoMetadata

Identifier and descriptive metadata for a single video, populated by the
data-pipeline worker.

| Field         | Type            | Notes                                     |
|---------------|-----------------|-------------------------------------------|
| `id`          | string (UUID)   | Stable hash of `source_url`               |
| `source_url`  | string          | Original video URL (YouTube)              |
| `title`       | string \| null  | Best-effort scrape; may be missing        |
| `author`      | string \| null  | @handle without leading `@`               |
| `duration_s`  | float           | Seconds; `0` if unknown                   |
| `downloaded`  | boolean         | Whether the local file is on disk         |
| `local_path`  | string \| null  | Relative path under `data/videos/`        |
| `tags`        | string[]        | Lowercase; may be empty                   |

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

## 9. Reproducibility envelope

Every `InferenceRun` MUST log:

- model id + version
- random seed
- video preprocessing params (resolution, fps, audio sample rate)
- TRIBE configuration hash
- wallclock timestamp (UTC)

This is non-negotiable — it is the difference between "ran a model" and "did
science with a model." The mock backend must also obey this so the
contract is exercised end-to-end before TRIBE arrives.

## 13. v2 Single-Video Job — YouTube URL + segment (CANONICAL)

Single source of truth for the v2 single-video product after the
**YouTube + timestamp refocus**. Supersedes the earlier "paste a TikTok
URL, analyze the whole clip" assumption. Three surfaces must agree:

| Surface                  | File                                              |
|--------------------------|---------------------------------------------------|
| Frontend client + picker | `apps/web/lib/api-v2.ts` + the `/single` page     |
| Orchestration            | `infra/aws/lambdas/jobs_create` (+ the chain)     |
| Inference                | `services/hf-space/app.py` (`POST /predict`)      |

### 13.1 Create a job

`POST /v2/jobs`

```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "startSec": 12.0,
  "endSec": 78.0
}
```

- `url` — a **YouTube** video URL. Accepted hosts: `youtube.com`,
  `www.youtube.com`, `m.youtube.com`, `youtu.be`. TikTok URLs are no
  longer accepted by this product.
- `startSec` — segment start in seconds from the video start; `>= 0`.
- `endSec` — segment end in seconds; `> startSec`.

The job analyzes **only the `[startSec, endSec)` window** — the Space
fetches just that segment with `yt-dlp --download-sections "*startSec-endSec"`,
never the whole video.

### 13.2 Validation (every layer re-validates — never trust the client)

The frontend pre-checks for instant feedback; the Lambda and the Space
both re-validate. Cheap, request-only checks reject **synchronously** at
`POST /v2/jobs` (HTTP 400 + `error_code`); the in-bounds check needs the
real video length so it is authoritative in the Space and surfaces as a
terminal **failed** job status.

| Rule                                       | `error_code`            | Where               |
|--------------------------------------------|-------------------------|---------------------|
| `url` is not a recognized YouTube URL      | `invalid_url`           | 400 at create       |
| `startSec < 0` or `startSec >= endSec`     | `bad_segment`           | 400 at create       |
| `endSec - startSec > 90`                   | `segment_too_long`      | 400 at create       |
| `endSec > <real video duration>`           | `segment_out_of_bounds` | failed status (Space reads duration via yt-dlp metadata **before** download) |

The **90-second ceiling is a hard cap** — the model + the cost/latency
budget are built around short stimuli. The frontend may also pre-check
the bound if it has the duration (e.g. via a YouTube oEmbed/metadata
call), but the server-side check is authoritative.

These join the existing terminal failure statuses (`failed_download`,
`failed_inference`) reported through normal job-status polling.

### 13.3 Everything downstream is unchanged

- `GET /v2/jobs/{id}` polling, the `JobStatus` vocabulary, and the result
  `ActivationPayload` are **unchanged** from the existing v2 flow.
- In the result, `videoDurationSec` is the **analyzed segment length**
  (`endSec - startSec`, after any auto-trim) — not the source video's
  full length.

### 13.4 Upload path (secondary)

The direct-MP4 upload path may remain as a secondary input (whole file,
still subject to the 90 s cap). Timestamp selection applies to the
YouTube-URL path only; an uploaded file is analyzed in full (≤90 s).
Keep it working; do not extend it with segment selection.

### 13.5 Implementation notes (resolves the Phase-1 worker questions)

- **The segment flows all the way to the Space.** The internal Lambda → Space
  `POST /predict` request gains `startSec` and `endSec` (top-level, alongside
  `source`/`callbackUrl`/`callbackToken`). `source.kind=="url"`'s value is a
  YouTube URL; `source.kind=="s3"` (upload) stays and ignores the segment.
- **Generic block status.** Rename `tiktok_blocked` → **`download_blocked`**
  everywhere it is emitted or consumed (Space `app.py` + `mock_local.py`, the
  AWS callback Lambda, the frontend's special-case). No longer TikTok-specific.
- **Accepted YouTube URL forms** (client validator + server): `youtube.com/watch?v=…`,
  `www.youtube.com/watch…`, `m.youtube.com/watch…`, `youtu.be/…`, and
  `youtube.com/shorts/…`. Anything else → `invalid_url`.
- **No auto-trim.** The previous 60–90 s auto-trim band is removed. The segment
  is taken verbatim from `[startSec, endSec)`; `endSec − startSec > 90` is
  rejected (`segment_too_long`) rather than silently trimmed. `MAX_DURATION_SEC`
  stays 90 as the hard ceiling; the `TRIM_THRESHOLD/TRIM_TARGET` logic goes away.
