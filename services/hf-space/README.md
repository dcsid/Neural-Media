---
title: Neural Media — TRIBE Inference
emoji: 🧠
colorFrom: indigo
colorTo: pink
sdk: docker
app_port: 7860
hardware: a10g-small
license: cc-by-nc-4.0
pinned: false
short_description: TRIBE v2 inference for single-video brain prediction
---

# neural-media · HuggingFace Space (TRIBE inference)

GPU service for the "single-video → brain" product.

Invoked by AWS Lambda over HTTP; runs `yt-dlp → ffmpeg → TRIBE v2 →
region aggregation` in a background task, then POSTs the gzipped
activation JSON back to a caller-supplied callback URL.

Free tier (ZeroGPU / A10G).  Per-call wall-clock budget is ~120 s; we
target < 90 s end-to-end.

## API

### `POST /predict` → `202 {accepted: true}`

Body:

```json
{
  "jobId": "string",
  "source": { "kind": "url", "value": "https://www.youtube.com/watch?v=…" },
  "startSec": 12.0,
  "endSec": 78.0,
  "callbackUrl": "https://...",
  "callbackToken": "shared-with-aws"
}
```

`source.kind` is `"url"` (a **YouTube** URL, analyzed over the
`[startSec, endSec)` window only — downloaded with
`yt-dlp --download-sections`, never the whole video) or `"s3"` (HTTPS GET of
a presigned upload; the whole file, segment ignored). The Space re-validates
the segment and rejects bad requests **synchronously** with
`400 { "detail": { "error_code" } }` — `invalid_url`, `bad_segment`, or
`segment_too_long` (window longer than 90 s). Otherwise it returns 202
immediately and runs the pipeline in the background.

### Callback (HF Space → AWS)

When the job finishes, the Space POSTs the `callbackUrl` with header
`X-NM-Token: <callbackToken>` and one of these bodies:

```json
{
  "jobId": "...",
  "status": "done",
  "activationsB64": "<gzip(JSON.stringify(activation)) then base64>",
  "durationSec": 66.0,
  "modelVersion": "<resolved HF commit sha>"
}
```

`durationSec` is the **analyzed segment length** (`endSec - startSec`), not
the source video's full length.

```json
{ "jobId": "...", "status": "failed_download", "error": "download_blocked" }
```

```json
{ "jobId": "...", "status": "failed_inference", "error": "..." }
```

```json
{ "jobId": "...", "status": "rejected_duration", "durationSec": 42.0,
  "error": "segment_out_of_bounds" }
```

```json
{ "jobId": "...", "status": "rejected_duration", "durationSec": 121.0,
  "error": "video is 121.0s; max accepted is 90s" }
```

`segment_out_of_bounds` is reported when `endSec` exceeds the real video
length (the Space reads it via yt-dlp metadata before downloading); the last
form is the upload path rejecting a whole file longer than 90 s.

The activation payload (before gzip+base64) matches the shared API
contract:

```json
{
  "videoDurationSec": 47.2,
  "timestamps": [0.0, 0.667, 1.333, ...],
  "byRegion": { "v1": [...], "v2": [...], "..." : [...], "vwfa": [...] },
  "modelVersion": "<sha>"
}
```

### `GET /healthz` / `GET /`

Liveness + introspection.

## Configuration

| Env var                  | Default          | Notes                                            |
| ------------------------ | ---------------- | ------------------------------------------------ |
| `CALLBACK_SHARED_SECRET` | *(required)*     | Refuses requests until set.                      |
| `HF_MAX_DURATION_SEC`    | `90`             | Hard cap on the analyzed window (segment or upload). |
| `ZERO_GPU_DURATION_SEC`  | `110`            | Budget passed to `@spaces.GPU(duration=...)`.    |
| `HF_HOME`                | `/data/hf-cache` | Model weight cache.                              |
| `LOG_LEVEL`              | `INFO`           | Standard Python logging level.                   |

Set `CALLBACK_SHARED_SECRET` as a Space secret (Settings → Repository
secrets) to the same value the AWS Lambda holds as `NM_CALLBACK_TOKEN`.

## Deploy

The Space is a separate git repo on HuggingFace (e.g.
`huggingface.co/spaces/<org>/neural-media-tribe`).  The deploy snapshot
needs **all three** of:

- this directory's files at the root,
- `services/inference/` (the editable install — TRIBE wrapper + region
  masks JSON), and
- `shared/` at the root (the contracts package that
  `services/inference/.../_shared.py` imports via a sys.path shim).

From the monorepo root:

```bash
# 1. Stage the Space tree in a temp dir.
STAGE=$(mktemp -d)
rsync -a --delete services/hf-space/  "$STAGE/"
mkdir -p "$STAGE/services"
rsync -a --delete services/inference/ "$STAGE/services/inference/"
rsync -a --delete shared/             "$STAGE/shared/"

# 2. Point at the HF Space remote (one-time, then it's cloned).
cd "$STAGE"
git init -q
git remote add space https://huggingface.co/spaces/<your-org>/neural-media-tribe
git add -A
git -c user.email=deploy@neural-media -c user.name=deploy \
    commit -q -m "deploy $(date -u +%Y%m%dT%H%M%SZ)"

# 3. Push.  HF will rebuild the Docker image automatically.
git push -f space HEAD:main
```

(Future iteration: wrap the above in a `scripts/deploy_hf_space.sh`.)

### Local smoke test

```bash
docker build -t nm-hf-space \
    -f services/hf-space/Dockerfile .             # build from monorepo root
docker run --rm -p 7860:7860 \
    -e CALLBACK_SHARED_SECRET=test \
    nm-hf-space

curl -X POST http://localhost:7860/predict \
    -H 'content-type: application/json' \
    -d '{"jobId":"smoke-1","source":{"kind":"url","value":"https://www.youtube.com/watch?v=jNQXAC9IVRw"},"startSec":0,"endSec":18,"callbackUrl":"http://host.docker.internal:9999/cb","callbackToken":"x"}'
```

Real-mode inference won't work locally without a CUDA GPU + the TRIBE
weights cache; for that case run with the mock backend by swapping the
import in `app.py` (only useful for callback-path testing).

## Scope of v1

- Region-mean timeseries only (`byRegion`).  No per-vertex (`perVertex`)
  field.
- No AWS-side code in this directory.  No frontend code.
- One in-flight job per process; ZeroGPU serialises GPU access per call.

See `docs/scientific-framing.md` in the monorepo for what these
predictions do and do not mean.
