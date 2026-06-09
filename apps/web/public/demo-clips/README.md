# demo-clips/

Self-hosted source video segments + posters for the gallery's
**"video beside the brain"** feature. Each clip is the exact
`[startSec, endSec)` window of the matching baked brain in
[`../demo-predictions/`](../demo-predictions/), so one slider can drive the
video and the 3D brain in sync.

## These assets are intentionally NOT committed

This repository is **public** and the clips are **third-party content**. The
generated `.mp4` and `.jpg` files are `.gitignore`d — only this `README.md`
and `.gitkeep` are tracked. Generate the assets locally instead of committing
them.

## Regenerate

```bash
python scripts/build_demo_clips.py            # build all entries
python scripts/build_demo_clips.py --only <slug> --force   # rebuild one
```

Requires `ffmpeg` and `yt-dlp` on the machine. Each `<slug>` matches an entry
in [`../demo-predictions/index.json`](../demo-predictions/index.json). For
every entry the script writes:

- `<slug>.mp4` — web-ready H.264 (yuv420p, `+faststart`, ~480p tall-side cap,
  CRF 28, low-bitrate AAC audio kept)
- `<slug>.jpg` — a mid-clip poster frame

The script is idempotent (existing, non-empty pairs are skipped unless
`--force`).
