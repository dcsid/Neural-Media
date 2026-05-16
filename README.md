# Neural Media

A local-first web app that runs your TikTok watch history through
Meta&nbsp;FAIR&nbsp;TRIBE&nbsp;v2 and reports the **predicted average human
cortical response** — a non-invasive, comparative read on what your
short-form media diet would look like through an fMRI lens. Spotify
Wrapped for a brain that is not yours.

## What this measures, plainly

**Does:** predicted BOLD fMRI response, averaged across TRIBE v2's 720
training subjects, on 20,484 cortical-surface vertices, per watched video.

**Does not:** measure *your* brain, addiction, reward circuitry,
attention, cognitive load, satisfaction, or how short-form media is
"rewiring" you. See [`docs/scientific-framing.md`](docs/scientific-framing.md).

> TRIBE v2 is licensed CC&nbsp;BY-NC. This project is non-commercial.

## Status

Scaffold. The integration lead has laid out the monorepo and the shared
contracts. Five workers are bringing up:

- **ml-inference** — TRIBE wrapper + mock backend + region aggregation
- **data-pipeline** — TikTok export importer + yt-dlp + preprocess
- **api-orchestrator** — FastAPI service + SQLite store
- **frontend-dashboard** — Next.js dashboard / detail / compare views
- **brain-viz** — 20,484-vertex cortical mesh in React Three Fiber

Briefs live under [`docs/worker-briefs/`](docs/worker-briefs).

## Layout

```
apps/
  web/                Next.js + TypeScript + Tailwind frontend
services/
  api/                FastAPI orchestrator (+ SQLite, +Parquet)
  inference/          TRIBE v2 wrapper (mock + real backends)
shared/               Canonical contracts — Pydantic + TS + Markdown
data/
  sample/             Committed fixtures: TikTok export, mock outputs
  videos/             Downloaded videos (gitignored)
  activations/        Raw NPZ/Parquet activations (gitignored)
  sqlite/             Local DB (gitignored)
docs/
  architecture.md
  scientific-framing.md
  worker-briefs/      One per worker
```

## Getting started

Prerequisites: Python 3.11+, Node 20+, pnpm or npm, optionally a CUDA
GPU for the real TRIBE backend.

```bash
make install          # installs all services + frontend
make sample           # generates mock inference outputs from the
                      # committed TikTok export fixture
make dev              # runs FastAPI on :8000 and Next.js on :3000
```

Open [http://localhost:3000](http://localhost:3000).

## Privacy

Everything runs on your machine. No analytics SDKs. Watch history never
leaves the device. `data/videos/` and `data/activations/` are
gitignored. See [`docs/scientific-framing.md`](docs/scientific-framing.md).

## License

Code under `apps/`, `services/`, `shared/`, `docs/`: CC&nbsp;BY-NC 4.0,
to match the TRIBE v2 weights this project depends on. Non-commercial
only.
