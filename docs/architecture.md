# Neural Media — Architecture

Local-first, single-user web app. Five major components live in this repo,
owned by five workers. The integration lead (terminal 1) owns the contracts
between them and the merge process — not the implementations.

## Components

```
+---------------------+      +-----------------+      +--------------------+
|  TikTok export      | ---> | data-pipeline   | ---> | services/inference |
|  (user-supplied)    |      |  (yt-dlp,       |      |  (TRIBE v2 + mock) |
+---------------------+      |   preprocess,   |      +--------------------+
                             |   SQLite write) |              |
                             +-----------------+              v
                                     |              +--------------------+
                                     |              | activations/*.npz  |
                                     v              | + *.meta.json      |
                             +-----------------+    +--------------------+
                             | services/api    | <-- region metrics, runs
                             |  (FastAPI)      |
                             +-----------------+
                                     |
                                     v  /api/v1/*
                             +-----------------+      +--------------------+
                             | apps/web        | ---> | brain-viz (R3F)    |
                             | (Next.js + TS)  |      | 20484-vertex mesh  |
                             +-----------------+      +--------------------+
```

Data flow per video:

1. **Importer** (data-pipeline) reads the user's TikTok export
   (`.json`, `.txt`, or `.zip`; auto-detected by extension), applies
   the optional half-open `[since, until)` time-window filter, and
   writes `WatchEvent` + `VideoMetadata` rows. Events outside the
   window never enter the queue, so the inference workload is capped
   at parse time before any I/O happens.
2. **Mode dispatch.** `mock` mode skips steps 3–4 entirely and goes
   straight to inference with `MockBackend`. `real` mode runs the
   full pipeline; the api worker's `/capabilities` endpoint pre-checks
   that the `[real]` extra + GPU + ffmpeg + yt-dlp are all available
   and blocks submission with a structured `error_code` if not.
3. **Downloader** (real mode only) fetches the video via yt-dlp with
   retry + jitter + UA rotation + dedup. Handles both
   `tiktok.com/@handle/video/<id>` and the newer
   `tiktokv.com/share/video/<id>/` URL forms.
4. **Preprocessor** (real mode only) normalizes resolution / FPS /
   audio sample rate to TRIBE's expected `224×224 @ 8 fps + 16 kHz`
   (imported directly from `DEFAULT_PREPROCESSING_PARAMS`).
5. **Inference runner** (ml-inference) calls TRIBE v2 (or
   `MockBackend` in mock mode), emits a `(num_timepoints, 20484)`
   fp32 tensor + a meta sidecar.
6. **Aggregator** (ml-inference) reduces vertices → 8 canonical
   regions and writes `RegionMetrics` rows.
7. **Optional cleanup** (data-pipeline) — opt-in via
   `--purge-after-inference` (drops raw + preprocessed `.mp4`s) and
   `--purge-activations` (additionally drops the per-vertex `.npz`
   and JSON sidecar, keeping only `RegionMetrics` rows). Tier-a keeps
   peak transient disk to ~10 MB; tier-b cuts steady-state from
   ~5 MB/video to ~30 KB/video.
8. **API** (api-orchestrator) serves all of the above under
   `/api/v1/*` plus the import write surface
   (`POST /api/v1/import`, `GET /api/v1/import/{id}`,
   `POST /api/v1/import/{id}/retry`) and meta surfaces
   (`/api/v1/capabilities`).
9. **Frontend** (frontend-dashboard + brain-viz) renders Dashboard,
   Video Detail, and Compare views. `MockModeBadge` surfaces whenever
   the displayed data is backed by `InferenceRun.model_id` starting
   with `tribe-v2-mock` — non-negotiable, see
   `docs/scientific-framing.md`.

## Boundaries

| Boundary                | Contract                                |
|-------------------------|-----------------------------------------|
| importer → DB           | `WatchEvent`, `VideoMetadata` rows      |
| inference → disk        | `activations/{run_id}.npz`, `.meta.json`|
| inference → DB          | `InferenceRun`, `RegionMetrics` rows    |
| API → frontend          | JSON endpoints under `/api/v1/`         |
| frontend → brain-viz    | `ActivationOutput.region_means` + keyframes |

All of these are defined exactly once in `shared/CONTRACTS.md` and mirrored
in `shared/schemas.py` (Pydantic) and `shared/types.ts` (TypeScript).

## Run topology

Local dev runs three processes:

| Process       | Port    | Owner                  |
|---------------|---------|------------------------|
| Next.js dev   | 3000    | frontend-dashboard     |
| FastAPI       | 8000    | api-orchestrator       |
| Inference     | in-proc | ml-inference (initially in-process; can split out behind FastAPI if needed) |

The frontend talks only to `/api/v1/*` — the Next config rewrites it to the
FastAPI service. The frontend MUST NOT touch the filesystem.

## Storage

- `data/sqlite/neural_media.db` — `videos`, `watch_events`, `inference_runs`
- `data/activations/{run_id}.npz` — raw activation tensors (or Parquet)
- `data/videos/` — downloaded video files (gitignored)
- `data/sample/` — committed fixtures (the TikTok export shape + mock outputs)

## Non-goals

- Multi-user / auth
- Cloud DB / hosted backend
- Real-time streaming inference
- Analytics SDKs (privacy is load-bearing — see scientific-framing.md)
