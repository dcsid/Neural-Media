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

1. **Importer** (data-pipeline) reads the user's TikTok export JSON, writes a
   `WatchEvent` and `VideoMetadata` row.
2. **Downloader** fetches the video via yt-dlp with retry + dedup.
3. **Preprocessor** normalizes resolution / FPS / audio sample rate.
4. **Inference runner** (ml-inference) calls TRIBE v2 (or the mock backend),
   emits a `(num_timepoints, 20484)` fp32 tensor + a meta sidecar.
5. **Aggregator** (ml-inference) reduces vertices → regions and writes
   `RegionMetrics` rows.
6. **API** (api-orchestrator) serves all of the above under `/api/v1/*`.
7. **Frontend** (frontend-dashboard + brain-viz) renders Dashboard, Video
   Detail, and Compare views.

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
