#!/usr/bin/env python3
"""Build self-hosted demo clips + posters for the gallery's "video beside the brain".

The gallery brains are already baked (apps/web/public/demo-predictions/). This
script self-hosts the matching *source segments* so one slider can drive the
video and the brain in tight sync. For each entry in that gallery's
``index.json`` ({slug, url, startSec, endSec, durationSec}):

    1. yt-dlp fetches the video head through endSec (``--download-sections``),
       capping the source height so we never pull a 4K master.
    2. ffmpeg accurately trims to ``[startSec, endSec)`` and re-encodes a
       web-ready H.264 MP4 — yuv420p, +faststart, height capped at ~480p,
       CRF 28, with the (low-bitrate) AAC audio kept.
    3. ffmpeg grabs a mid-clip frame as a poster JPEG.

Outputs: ``apps/web/public/demo-clips/<slug>.mp4`` + ``<slug>.jpg``. Those
assets are ``.gitignore``d and intentionally NOT committed — this repo is
public and the clips are third-party content. Regenerate them with this script.

Idempotent: a slug whose .mp4 and .jpg both already exist (non-empty) is
skipped unless ``--force``. Per-clip progress + a final summary, mirroring
scripts/build_demo_gallery.py.

Usage:
    python scripts/build_demo_clips.py [--force] [--only SLUG]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_JSON = REPO_ROOT / "apps" / "web" / "public" / "demo-predictions" / "index.json"
OUTPUT_DIR = REPO_ROOT / "apps" / "web" / "public" / "demo-clips"

# Web-ready encode knobs. These are asset-prep choices, not a contract.
TARGET_HEIGHT = 480          # cap the tall side at ~480p; downscale-only, keep aspect
VIDEO_CRF = 28
VIDEO_PRESET = "veryfast"
AUDIO_BITRATE = "96k"        # keep the audio track, at a low bitrate
POSTER_QUALITY = 3           # ffmpeg -q:v for the JPEG (2 = best … 31 = worst)
# Pad the downloaded tail a couple seconds past endSec so the keyframe-aligned
# section always covers endSec; the precise cut happens in ffmpeg.
DOWNLOAD_TAIL_PAD_SEC = 2.0


# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------

def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        sys.exit(f"error: {binary} not found on PATH (install ffmpeg)")
    return path


def _ytdlp_base() -> list[str]:
    """Prefer the yt-dlp CLI; fall back to `python -m yt_dlp` (it's a dep of
    services/inference's [real]/pipeline extras but often not on PATH)."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    sys.exit("error: yt-dlp not found — `pip install yt-dlp` or put it on PATH")


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _ffprobe_dims(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def _target_scale(w: int, h: int) -> tuple[int, int]:
    """Downscale-only so the height (tall side) is ≤ TARGET_HEIGHT, preserving
    aspect, with both dimensions even (required by yuv420p / libx264)."""
    if h > TARGET_HEIGHT:
        w = round(w * TARGET_HEIGHT / h)
        h = TARGET_HEIGHT
    w -= w % 2
    h -= h % 2
    return max(2, w), max(2, h)


# ---------------------------------------------------------------------------
# yt-dlp + ffmpeg steps
# ---------------------------------------------------------------------------

def _download_head(url: str, end_sec: float, dest_dir: Path) -> Path:
    """Fetch the video from its start through ``end_sec`` (+ a small pad).

    Downloading ``[0, endSec]`` (rather than the whole video) keeps absolute
    timestamps intact from 0, so the ffmpeg seek below lands on the true
    startSec — while still skipping the (potentially long) tail. Height is
    capped at the source level so a 4K master never gets pulled.
    """
    out_template = str(dest_dir / "src.%(ext)s")
    section = f"*0-{end_sec + DOWNLOAD_TAIL_PAD_SEC:g}"
    fmt = (
        f"bv*[height<={TARGET_HEIGHT}]+ba/b[height<={TARGET_HEIGHT}]"
        f"/bv*+ba/b"
    )
    cmd = _ytdlp_base() + [
        "--quiet", "--no-warnings", "--no-playlist", "--no-part",
        "--download-sections", section,
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-400:].strip()
        raise RuntimeError(f"yt-dlp failed: {tail}")
    for cand in sorted(dest_dir.glob("src.*")):
        if cand.is_file() and cand.stat().st_size > 0:
            return cand
    raise RuntimeError("yt-dlp produced no output file")


def _encode_clip(src: Path, dest_mp4: Path, *, start: float, end: float) -> None:
    """Accurately trim [start, end) and re-encode to a web-ready H.264 MP4."""
    w, h = _ffprobe_dims(src)
    tw, th = _target_scale(w, h)
    # Keep the real extension on the temp file so ffmpeg infers the muxer
    # (a bare ".tmp" makes ffmpeg fail with "Invalid argument").
    tmp = dest_mp4.with_name(dest_mp4.stem + ".tmp" + dest_mp4.suffix)
    cmd = [
        _require("ffmpeg"), "-y", "-loglevel", "error",
        "-i", str(src),
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",   # output-side = frame-accurate
        "-vf", f"scale={tw}:{th}:flags=bicubic",
        "-c:v", "libx264", "-preset", VIDEO_PRESET, "-crf", str(VIDEO_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", AUDIO_BITRATE,
        "-movflags", "+faststart",
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg encode failed: {proc.stderr[-300:].strip()}")
    tmp.replace(dest_mp4)


def _make_poster(clip_mp4: Path, dest_jpg: Path, *, clip_dur: float) -> None:
    """Grab a single mid-clip frame as the poster image."""
    tmp = dest_jpg.with_name(dest_jpg.stem + ".tmp" + dest_jpg.suffix)
    cmd = [
        _require("ffmpeg"), "-y", "-loglevel", "error",
        "-ss", f"{clip_dur / 2.0:.3f}", "-i", str(clip_mp4),
        "-frames:v", "1", "-q:v", str(POSTER_QUALITY),
        str(tmp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg poster failed: {proc.stderr[-300:].strip()}")
    tmp.replace(dest_jpg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _load_entries() -> list[dict[str, Any]]:
    if not INDEX_JSON.exists():
        sys.exit(f"error: gallery index not found at {INDEX_JSON}")
    return json.loads(INDEX_JSON.read_text(encoding="utf-8")).get("entries", [])


def _human_size(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{n / 1_000:.0f} KB"


def build(*, force: bool, only: str | None) -> int:
    entries = _load_entries()
    if only:
        entries = [e for e in entries if e.get("slug") == only]
        if not entries:
            sys.exit(f"error: no entry with slug {only!r} in {INDEX_JSON.name}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    made = skipped = failed = 0
    total = len(entries)

    for i, entry in enumerate(entries, 1):
        slug = entry["slug"]
        url = entry["url"]
        start = float(entry["startSec"])
        end = float(entry["endSec"])
        dur = end - start
        mp4 = OUTPUT_DIR / f"{slug}.mp4"
        jpg = OUTPUT_DIR / f"{slug}.jpg"

        print(f"\n[{i}/{total}] clip: {entry.get('label', slug)}  "
              f"[{start:g},{end:g}) → {dur:g}s", flush=True)

        if not force and mp4.is_file() and mp4.stat().st_size > 0 \
                and jpg.is_file() and jpg.stat().st_size > 0:
            skipped += 1
            print(f"  [{'skip (exists)':32s}] {slug}.mp4 + .jpg", flush=True)
            continue

        try:
            with tempfile.TemporaryDirectory(prefix=f"clip-{slug}-") as td:
                src = _download_head(url, end, Path(td))
                src_dur = _ffprobe_duration(src)
                _encode_clip(src, mp4, start=start, end=end)
                _make_poster(mp4, jpg, clip_dur=dur)
            out_dur = _ffprobe_duration(mp4)
            made += 1
            print(
                f"  [{'ok':32s}] {slug}.mp4 ({_human_size(mp4.stat().st_size)}, "
                f"{out_dur:.1f}s) + .jpg   [src head {src_dur:.1f}s]",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 — keep going; report at the end
            failed += 1
            mp4.unlink(missing_ok=True)
            jpg.unlink(missing_ok=True)
            print(f"  [{'FAILED':32s}] {slug}: {exc}", flush=True)

    try:
        where: Path | str = OUTPUT_DIR.relative_to(REPO_ROOT)
    except ValueError:
        where = OUTPUT_DIR
    print()
    print(f"wrote clips to {where}/")
    print(f"  made: {made}   skipped: {skipped}   failed: {failed}   (of {total})")
    if made or skipped:
        print("  NOTE: these .mp4/.jpg are .gitignore'd — do not commit them.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true",
                        help="re-generate even if the .mp4/.jpg already exist")
    parser.add_argument("--only", metavar="SLUG", default=None,
                        help="build just one entry by slug")
    args = parser.parse_args()
    return build(force=args.force, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
