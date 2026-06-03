# Neural Media — Architecture

A single-purpose web app: paste a YouTube URL, pick a ≤90-second segment, and
see the **predicted average human cortical response** to that clip, rendered on
a 3D brain. One model (Meta FAIR TRIBE v2), two surfaces — a live "run your own
clip" path and a precomputed gallery of examples.

## Flow

```
[Browser:  /  and  /gallery]
   │  POST /v2/jobs { url, startSec, endSec }        GET /v2/jobs/{id} (poll)
   ▼                                                        ▲
[AWS API Gateway]                                           │ resultUrl
   │                                                        │
   ├─► [Lambda jobs_create] ──► [DynamoDB jobs table] ──────┘
   │            │ async invoke
   ▼            ▼
        [Lambda jobs_worker] ──► [HF Space  POST /predict]
                                     │  yt-dlp --download-sections (segment only)
                                     │  ffmpeg → TRIBE v2 → 8-region aggregate
                                     │  POST /v2/internal/hf-callback
                                     ▼
                               [Lambda hf_callback] ──► [S3 results/{id}.json.gz]
                                                     └─► [DynamoDB] mark done
```

The 3D brain (`apps/web/components/brain/`) renders the result's per-region
activation timeseries; the timeline scrubber plays it across the ≤90 s clip.

## The two surfaces

| | Live single-video | Gallery |
|---|---|---|
| Route | `/` | `/gallery` |
| Input | a YouTube URL + a chosen segment | a precomputed example, clicked |
| Compute | live, on the HF Space GPU, on demand | baked once offline; served as static JSON |
| Reliability | best-effort (needs the deployed Space) | instant, can't fail |

The gallery makes the demo bulletproof; the live path proves it's a real system.

## Where the model runs

TRIBE v2 (LLaMA-3.2 + V-JEPA2 + Wav2Vec-BERT under the hood) needs a GPU, so it
runs on a **HuggingFace Space**, never on AWS. AWS does only the lightweight
orchestration — accept the job, track status, store + serve the small result
JSON. The same Space pipeline is reused offline to bake the gallery.

## Boundaries

| Boundary | Contract |
|---|---|
| Browser → AWS | `POST /v2/jobs` + `GET /v2/jobs/{id}` — `CONTRACTS.md` §13 |
| Lambda → Space | `POST /predict { jobId, source, startSec, endSec, callbackUrl, callbackToken }` |
| Space → Lambda | callback `{ jobId, status, activationsB64, durationSec, modelVersion }` |
| Space/bake → brain | `ActivationPayload` (`byRegion` timeseries + `timestamps`) |

All defined once in [`../shared/CONTRACTS.md`](../shared/CONTRACTS.md).

## Layout

```
apps/web/             Next.js + R3F frontend (single-video page, gallery, brain mesh)
services/inference/   TRIBE v2 wrapper (mock + real) + 8-region aggregation
services/hf-space/    GPU inference service: download segment → TRIBE → callback
services/pipeline/    Clip fetch + preprocess utilities (yt-dlp, ffmpeg)
infra/aws/            SAM: API Gateway + Lambda chain + S3 + DynamoDB
shared/               Canonical contracts (Markdown + Pydantic + TypeScript)
```

## Non-goals

- Multi-user / auth / accounts.
- Long videos — the model + cost/latency budget target short stimuli (90 s hard cap).
- A persistent always-on GPU — the Space allocates on demand; the gallery is precomputed.
