# Neural Media

A local-first web app that runs your TikTok watch history through
Meta&nbsp;FAIR&nbsp;TRIBE&nbsp;v2 and reports the **predicted average human
cortical response** — a non-invasive, comparative read on what your
short-form media diet looks like through an fMRI lens. Spotify Wrapped
for a brain that is not yours.

<!-- 60-second demo: replace this comment with an mp4/gif under docs/
     once frontend-dashboard ships the screencast. -->

## What this measures, plainly

**Does:** predicted BOLD fMRI response, averaged across TRIBE v2's 720
training subjects, on 20,484 cortical-surface vertices, per watched
video. Aggregated into eight well-localised cortical regions (V1–V4,
auditory cortex, language network, FFA, VWFA).

**Does not:** measure *your* brain, addiction, reward circuitry,
attention, cognitive load, satisfaction, or how short-form media is
"rewiring" you. See [`docs/scientific-framing.md`](docs/scientific-framing.md)
for the full constraint list.

> TRIBE v2 is licensed CC&nbsp;BY-NC. This project is non-commercial.

## Running the demo

Prerequisites: Python 3.11+, Node 20+, pnpm.

```bash
make install            # installs all three Python services + the web app
make init-db            # provisions the local SQLite catalogue
make dev-api &          # FastAPI on 127.0.0.1:8000 (background)
make dev-web            # Next.js on 127.0.0.1:3000
```

Open [http://localhost:3000](http://localhost:3000):

1. **No videos yet.** Land on the empty state. Click "Drop your TikTok
   export".
2. **Drag-and-drop.** On the `/import` page, drag the file from your
   TikTok export onto the zone. Either the raw `user_data.json` or the
   full `.zip` archive works — both are accepted, the latter is read
   in memory at any depth without extraction.
3. **~3 seconds later.** The import job runs in the background under
   mock mode (no yt-dlp, no ffmpeg, no GPU). When status flips to
   complete you're redirected to the dashboard.
4. **Dashboard.** Region-balance bars, hour-of-day histogram, watched
   videos list.
5. **Click a video.** Per-region readings (mean / peak / sustained),
   per-region sparkline timeseries, and the 20,484-vertex cortical
   mesh with a timeline scrubber. Hover any vertex to see which region
   it belongs to and its predicted activation.

Where to get the TikTok export: [tiktok.com/setting/download-your-data](https://www.tiktok.com/setting/download-your-data).

### Exploring without a TikTok export

Drop the committed sample export at
`data/sample/tiktok_export/user_data.json` onto `/import` — 8 fake
videos populate the catalogue with deterministic mock activations.

### Real TRIBE v2 (GPU required)

```bash
pip install -e 'services/inference[real]'
# then on the /import page select "real" mode, or:
curl -X POST http://127.0.0.1:8000/api/v1/import \
  -F file=@user_data.json -F mode=real
```

Real mode requires accepting the TRIBE v2 (CC&nbsp;BY-NC) and the
transitive LLaMA 3.2 licenses on HuggingFace. ~16 GB VRAM minimum.

## Architecture

```
TikTok export → importer → yt-dlp → ffmpeg → TRIBE v2 → aggregator → API → Next.js + R3F
                                            └── MockBackend (no GPU) ──┘
```

Local-first, single-user, single-process. SQLite + on-disk NPZ for
activations. The FastAPI service binds to `127.0.0.1` only and rejects
`Host` headers other than `localhost`. No telemetry. No analytics SDKs.
Watch history never leaves the machine.

Full detail in [`docs/architecture.md`](docs/architecture.md).
[`shared/CONTRACTS.md`](shared/CONTRACTS.md) is the single source of
truth for data shapes that cross service boundaries.

## Layout

```
apps/web/                  Next.js + TypeScript + Tailwind frontend
services/
  api/                     FastAPI orchestrator (+ SQLite, +Parquet)
  inference/               TRIBE v2 wrapper (mock + real backends)
  pipeline/                Importer + yt-dlp + ffmpeg + orchestrator
shared/                    Canonical contracts — Pydantic + TS + Markdown
data/
  sample/                  Committed fixtures: TikTok export, mock outputs
  videos/                  Downloaded videos (gitignored)
  activations/             Raw NPZ activations (gitignored)
  sqlite/                  Local catalogue DB (gitignored)
  imports/                 Staged TikTok uploads (gitignored)
docs/
  architecture.md
  scientific-framing.md
  worker-briefs/           One per build-team worker
```

## Privacy

Everything runs on your machine. No analytics SDKs. Watch history never
leaves the device. `data/videos/`, `data/activations/`, `data/sqlite/`,
and `data/imports/` are all `.gitignore`d. See
[`docs/scientific-framing.md`](docs/scientific-framing.md).

## License

Code under `apps/`, `services/`, `shared/`, `docs/`: CC&nbsp;BY-NC 4.0,
to match the TRIBE v2 weights this project depends on. Non-commercial
only.
