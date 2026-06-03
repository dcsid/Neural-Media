# Neural Media

Paste a YouTube link, pick a moment up to 90 seconds, and watch a 3D brain
light up with the **predicted average human cortical response** to that
clip — a non-invasive, comparative read on what a piece of video does to the
visual, auditory, language, and face-processing parts of the brain. Built on
Meta FAIR's **TRIBE v2**.

<!-- 30-second demo: replace with an mp4/gif once the build lands. -->

## What this measures, plainly

**Does:** predicted BOLD fMRI response, averaged across TRIBE v2's 720
training subjects, on 20,484 cortical-surface vertices, for the chosen
segment of a video. Aggregated into eight well-localised cortical regions
(V1–V4, auditory cortex, language network, FFA, VWFA).

**Does not:** measure *your* brain, addiction, reward circuitry, attention,
cognitive load, satisfaction, or how media is "rewiring" you. See
[`docs/scientific-framing.md`](docs/scientific-framing.md) for the full
constraint list. Prefer comparative claims ("clip A predicts higher V1 than
clip B") over absolute ones.

> TRIBE v2 is licensed CC&nbsp;BY-NC. This project is non-commercial.

## Try it

- **The gallery** — a set of precomputed example clips. Click one and the
  brain animates instantly — no waiting, no setup, can't fail.
- **Your own clip** — paste a YouTube URL, set the start/end of a window up
  to 90 seconds, and run it live against the model.

<!-- Live demo link goes here once deployed. -->

## How it works

```
[Browser: YouTube URL + [startSec, endSec]]
      │  POST /v2/jobs
      ▼
[AWS API Gateway → Lambda chain] ──► [HuggingFace Space (GPU)]
                                          │  yt-dlp --download-sections (only the window)
                                          │  ffmpeg → TRIBE v2 → 8-region aggregate
[Browser polls GET /v2/jobs/{id}]  ◄──────┘  result → S3 + DynamoDB
      ▼
[3D cortical mesh + timeline scrubber]
```

Only the selected `[startSec, endSec)` window is ever downloaded or
processed — the model and the cost/latency budget are built around short
stimuli, so clips are capped at **90 seconds**. The precomputed gallery is
baked once (offline) and served as static JSON, so the example experience is
instant and reliable; the "paste your own" path runs live against the
deployed GPU Space.

[`shared/CONTRACTS.md`](shared/CONTRACTS.md) §13 is the single source of truth
for the job request shape and validation rules.

## Layout

```
apps/web/             Next.js + TypeScript + React-Three-Fiber frontend
                      (the single-video page, the gallery, the 3D brain mesh)
services/inference/   TRIBE v2 wrapper (mock + real) + 8-region aggregation
services/hf-space/    GPU inference service: download segment → TRIBE → callback
services/pipeline/    Clip fetch + preprocess utilities (yt-dlp, ffmpeg)
infra/aws/            AWS SAM: API Gateway + Lambda chain + S3 + DynamoDB
shared/               Canonical contracts (Markdown + Pydantic + TypeScript)
docs/                 Architecture, scientific framing, deploy + GPU runbooks
```

## Running and deploying

Local dev and the AWS + HuggingFace deploy are in
[`docs/single-video-deploy.md`](docs/single-video-deploy.md); the preselected
gallery is baked via the one-time GPU run in
[`docs/gpu-verification-and-gallery-bake.md`](docs/gpu-verification-and-gallery-bake.md).

> _Draft: exact run commands firm up as the YouTube + timestamp build lands._

## License

Code and docs: CC&nbsp;BY-NC 4.0, to match the TRIBE v2 weights this project
depends on. Non-commercial only. TRIBE v2 uses Llama 3.2 internally — built
with Llama.
