#!/usr/bin/env python3
"""One-clip real-pipeline smoke test — run BEFORE the full gallery bake.

Pushes a single short (~5s) YouTube segment through the real pipeline
(download -> ffmpeg -> TRIBE -> 8-region aggregate) and prints the output
shape. The point is to surface a broken GPU / missing weights / stale
yt-dlp extractor in *one clip's* time instead of 10 minutes into a bake.

It reuses the exact production path (``predict_one_url._run_pipeline``), so
a green smoke here means the bake's per-clip step works. ``--mock`` swaps in
MockBackend so CI can run the whole flow with no GPU, torch, or weights.

Usage::

    python scripts/smoke_gpu_clip.py                  # default clip, real TRIBE
    python scripts/smoke_gpu_clip.py --mock           # MockBackend (CI-friendly)
    python scripts/smoke_gpu_clip.py <youtube-url> --start-sec 0 --end-sec 5

Exit codes::

    0  pipeline ran and the output shape is well-formed (T >= 1, the 8
       canonical regions, every region series length == T)
    1  download failed (prints "download_blocked" if the host gated it)
    2  rejected input (invalid_url / bad_segment / segment_too_long)
    3  inference / aggregation failed, or a malformed output shape
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path shim — mirror predict_one_url so a fresh clone works without
# `pip install -e`. scripts/ is on the path so we can reuse predict_one_url.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    _REPO_ROOT,
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "services" / "inference",
    _REPO_ROOT / "services" / "pipeline",
):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import predict_one_url  # noqa: E402 — reuse the exact production pipeline

from neural_media_inference._shared import REGION_IDS  # noqa: E402

from neural_media_pipeline.downloader import (  # noqa: E402
    DownloadError,
    SegmentError,
    is_supported_youtube_url,
    validate_segment,
)

# A stable, public YouTube video + a tiny segment. dQw4w9WgXcQ is the
# canonical long-lived example used in CONTRACTS.md §13.
DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
DEFAULT_START_SEC = 0.0
DEFAULT_END_SEC = 5.0


def run_smoke(url: str, start_sec: float, end_sec: float, *, mock: bool) -> dict:
    """Run ONE clip through the real pipeline; return a shape summary.

    Raises the same typed errors as the predictor (``SegmentError``,
    ``DownloadError``, …) so :func:`main` can map them to exit codes.
    """
    validate_segment(start_sec, end_sec)
    payload = predict_one_url._run_pipeline(
        url, mock=mock, max_duration_sec=None, segment=(start_sec, end_sec),
    )
    by_region = payload["byRegion"]
    n_t = len(payload["timestamps"])
    regions = sorted(by_region)
    return {
        "num_timepoints": n_t,
        "regions": regions,
        "num_regions": len(regions),
        "series_lengths_uniform": all(len(s) == n_t for s in by_region.values()),
        "regions_match_contract": set(regions) == set(REGION_IDS),
        "videoDurationSec": payload["videoDurationSec"],
        "modelVersion": payload["modelVersion"],
    }


def _shape_ok(info: dict) -> bool:
    return (
        info["num_timepoints"] >= 1
        and info["series_lengths_uniform"]
        and info["regions_match_contract"]
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="smoke_gpu_clip.py", description=__doc__)
    p.add_argument(
        "url", nargs="?", default=DEFAULT_URL,
        help="YouTube URL (default: a stable public clip)",
    )
    p.add_argument("--start-sec", type=float, default=DEFAULT_START_SEC)
    p.add_argument("--end-sec", type=float, default=DEFAULT_END_SEC)
    p.add_argument(
        "--mock", action="store_true",
        help="use MockBackend (no GPU/torch); the real download still runs "
             "unless its seams are stubbed — CI stubs them.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    if not is_supported_youtube_url(args.url):
        print("invalid_url", file=sys.stderr)
        print(f"not a supported YouTube URL: {args.url}", file=sys.stderr)
        return 2
    try:
        start_sec, end_sec = validate_segment(args.start_sec, args.end_sec)
    except SegmentError as exc:
        print(exc.code, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    try:
        info = run_smoke(args.url, start_sec, end_sec, mock=args.mock)
    except SegmentError as exc:  # defense in depth
        print(exc.code, file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2
    except DownloadError as exc:
        if predict_one_url._looks_download_blocked(exc):
            print("download_blocked", file=sys.stderr)
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — backend / aggregate / ffprobe etc.
        print(f"inference failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    backend = "MockBackend" if args.mock else "TRIBE"
    print(f"clip:        {args.url}  [{start_sec:g}, {end_sec:g})")
    print(f"backend:     {backend}  (modelVersion={info['modelVersion']})")
    print(f"duration:    {info['videoDurationSec']:.2f}s")
    print(f"shape:       T={info['num_timepoints']} timepoints x {info['num_regions']} regions")
    print(f"regions:     {', '.join(info['regions'])}")

    if not _shape_ok(info):
        print("\nMalformed output shape (see above) — do NOT bake.", file=sys.stderr)
        return 3
    print("\nsmoke OK — the per-clip pipeline works; safe to run the full bake.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
