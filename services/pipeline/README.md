# neural-media-pipeline — clip-fetcher

Fetch a single video and normalize it to the shape TRIBE v2 expects.
**Local-first: nothing leaves the machine**, and outbound network access is
confined to the downloader (yt-dlp).

This package is deliberately small — two stages, no orchestration, no
catalog. It is the front of the single-video demo path: a caller hands it a
URL, it returns a normalized `.mp4` ready for inference. The demo-gallery
bake tooling in the repo-root `scripts/` (`predict_one_url.py`,
`build_demo_gallery.py`) is the reference consumer.

```
 (url) ──▶ downloader.download_video ──▶ data/videos/<id>.mp4
                                              │
                                              ▼
          preprocess.preprocess_video ──▶ data/videos_processed/<id>.mp4
                                              │
                                              ▼
                                   (handed to neural_media_inference)
```

| Module | Responsibility |
|--------|----------------|
| [`downloader.py`](neural_media_pipeline/downloader.py) | One yt-dlp seam with full-jitter exponential backoff, User-Agent rotation, and an on-disk dedup cache. Never touches the network on a cache hit; a zero-byte leftover is re-fetched. |
| [`preprocess.py`](neural_media_pipeline/preprocess.py) | ffmpeg normalization to TRIBE v2's input shape. Target resolution/fps/audio-rate are imported from `neural_media_inference` so they cannot drift. |

## Usage

```python
from neural_media_pipeline import (
    DownloadConfig, download_video,
    PreprocessConfig, preprocess_video,
)
from shared.schemas import VideoMetadata

video = VideoMetadata(id="my-clip", source_url="https://www.youtube.com/watch?v=…")

dl = download_video(video, DownloadConfig(videos_dir="data/videos"))
pre = preprocess_video(dl.local_path, video.id, PreprocessConfig(processed_dir="data/videos_processed"))
# pre.local_path is the normalized mp4, ready for run_inference(...)
```

Both stages are idempotent on disk (a non-empty output is a cache hit) and
side-effect-free under their injectable seams (`fetch=` / `run=`), so callers
and tests can stub the network and ffmpeg.

### `probe_share_url.py`

[`scripts/probe_share_url.py`](scripts/probe_share_url.py) makes a single
real yt-dlp call to confirm a share-shortlink still resolves — a manual
smoke check when yt-dlp ships an extractor change. It is the only code here
that hits the network on purpose; run it by hand, not in CI.

## Error handling

- **Download retries.** Per-attempt failures retry with full-jitter backoff
  up to `max_attempts`; only an exhausted budget raises `DownloadError`.
  `download_batch` captures a per-video failure as a result row by default.
- **Preprocess guards.** A missing/non-regular source (`src.is_file()`) and a
  missing `ffmpeg` binary both fail with a clear message before any work; a
  silent zero-byte ffmpeg output is surfaced as `PreprocessError`.
- **Capability probes.** `yt_dlp_available()` / `ffmpeg_available()` let
  callers degrade cleanly when the external tools aren't installed.

Privacy: no module ever logs a full source URL — the stable id (a hash of
the URL) is the only handle that appears in logs. State-changing log lines
follow the `event=<name> key=value` convention.

## Testing

```bash
cd services/pipeline
../../.venv-dev/bin/python -m pytest -q     # PYTHONPATH is set by `make test`
```

Tests never touch the network, ffmpeg, or a GPU — every external side effect
is an injectable callable the suite replaces with a deterministic fake. New
behavior should land with a regression test in [`tests/`](tests/).
