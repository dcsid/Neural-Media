#!/usr/bin/env python3
"""Local single-URL predictor — TikTok / video URL → brain activation JSON.

This is the offline counterpart of ``services/hf-space/app.py``: it
runs the same download → preprocess → TRIBE → aggregate pipeline
against one URL on the local machine, and emits an activation JSON
in the *exact* shape the HF Space POSTs to its callback.  Useful for
plumbing the whole stack before cutting a HuggingFace / AWS deploy.

Output contract (matches services/hf-space/app.py:_run_job → activation_payload):

    {
      "videoDurationSec": float,
      "timestamps":       [float, ...],   # length T, sampled at 1.5 Hz
      "byRegion":         { "v1": [...], ..., "vwfa": [...] },
      "modelVersion":     str
    }

Usage:

    python scripts/predict_one_url.py <url> [--output PATH] [--mock] [--max-duration-sec 60]

Exit codes:

    0  success
    1  download failed (prints "tiktok_blocked" to stderr if TikTok 403'd
       — wrapping scripts can grep for that token)
    2  video duration exceeds --max-duration-sec (default unlimited)
    3  inference / aggregation failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path shim
# ---------------------------------------------------------------------------
# The two service packages live under services/<name>/ and aren't pip-
# installed by default.  Mirror the import shim that
# services/inference/.../_shared.py uses so a fresh checkout works
# without `pip install -e`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    _REPO_ROOT,
    _REPO_ROOT / "services" / "inference",
    _REPO_ROOT / "services" / "pipeline",
):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

# All third-party / local imports go below the path shim.
import numpy as np  # noqa: E402

from shared.schemas import VideoMetadata  # noqa: E402

from neural_media_inference.aggregate import (  # noqa: E402
    WIRE_TIMESTAMP_DECIMALS,
    aggregate_region_metrics,
)
from neural_media_inference.backend import MockBackend  # noqa: E402
from functools import partial  # noqa: E402

from neural_media_pipeline.downloader import (  # noqa: E402
    DownloadConfig,
    DownloadError,
    _yt_dlp_fetch,
    download_video,
)
from neural_media_pipeline.preprocess import (  # noqa: E402
    PreprocessConfig,
    PreprocessError,
    preprocess_video,
)


_log = logging.getLogger("predict-one-url")

# Sampling rate of the activation timeseries.  TRIBE samples at 1.5 Hz
# (CONTRACTS.md §4); matched by services/hf-space/app.py:_build_timestamps.
SAMPLE_RATE_HZ = 1.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stable_video_id(url: str) -> str:
    """Hash the URL into a 16-char stable id.

    Same id derivation we use elsewhere when we don't have a real
    catalogue row to anchor to.  Stable across runs so re-running the
    script on the same URL hits the download cache (when configured to
    use one — we don't here, but the property is still nice).
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _ffprobe_duration(path: Path) -> float:
    """Read the media duration in seconds via ``ffprobe``.

    Raises ``RuntimeError`` if ffprobe is missing or returns non-numeric
    output — the caller turns those into a clean stderr message.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe not found on PATH (install ffmpeg)")
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False, capture_output=True,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-200:].decode("utf-8", errors="replace")
        raise RuntimeError(f"ffprobe exited {proc.returncode}: {tail}")
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned non-numeric duration: {proc.stdout!r}") from exc


def _build_timestamps(num_timepoints: int) -> list[float]:
    """One timestamp per timepoint, in seconds — length ``num_timepoints``.

    Derived from the actual timepoint count the backend produced so this
    array is always the same length as each ``byRegion`` series (the
    invariant apps/web/lib/api-v2.ts enforces). Rounded to the shared
    ``WIRE_TIMESTAMP_DECIMALS`` so the two offline scripts (this one and
    build_demo_gallery.py) emit identical precision. The field *shape*
    matches services/hf-space/app.py's callback payload; its deployed
    counterpart happens to round wider, which is cosmetic — the frontend
    validates lengths and types, not decimal places.
    """
    if num_timepoints <= 0:
        return []
    dt = 1.0 / SAMPLE_RATE_HZ
    return [round(i * dt, WIRE_TIMESTAMP_DECIMALS) for i in range(num_timepoints)]


def _seed_from_url(url: str) -> int:
    """Derive a 31-bit positive seed deterministically from the URL.

    Same property the HF Space relies on: identical input → identical
    output, no hidden randomness.
    """
    digest = hashlib.sha256(url.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _looks_tiktok_blocked(err: BaseException) -> bool:
    """Sniff a download failure for TikTok-block signatures.

    yt-dlp doesn't surface a single typed exception; the cause chain
    carries the original message.  We keep the substring list small and
    high-signal so a real connectivity error isn't mislabelled.
    """
    seen: set[int] = set()
    cur: BaseException | None = err
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).lower()
        if "tiktok" in msg and any(
            sig in msg
            for sig in ("403", "blocked", "captcha", "login required", "rate")
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def _build_backend(*, mock: bool, videos_dir: Path):
    """Instantiate the requested inference backend.

    MockBackend has no init-side requirements.  TribeBackend pulls
    torch + tribev2 (multi-GB) lazily; we import inside this branch so
    a ``--mock`` invocation works on a stock laptop without torch.
    """
    if mock:
        return MockBackend()
    # Heavy imports gated to the real path.
    import torch  # type: ignore[import-not-found]  # noqa: PLC0415

    from neural_media_inference.backend_tribe import TribeBackend  # noqa: PLC0415

    device = "cuda" if torch.cuda.is_available() else "cpu"
    _log.info("loading TribeBackend (device=%s) — this may take a while on first run", device)
    return TribeBackend(
        videos_dir=videos_dir,
        device=device,
        accept_licenses=True,
    )


def _run_pipeline(
    url: str,
    *,
    mock: bool,
    max_duration_sec: float | None,
) -> dict:
    """Execute download → preprocess → infer → aggregate and return the
    wire-format activation payload.  Raises with a typed sentinel on
    duration rejection so the CLI can map to exit code 2."""
    video_id = _stable_video_id(url)
    workdir = Path(tempfile.mkdtemp(prefix=f"predict-{video_id}-"))
    _log.info("workdir=%s", workdir)
    try:
        raw_dir = workdir / "raw"
        proc_dir = workdir / "processed"
        raw_dir.mkdir()
        proc_dir.mkdir()

        # 1. Download.  We deliberately don't expose the on-disk cache
        #    (data/videos/) because the brief says not to write there;
        #    each invocation gets its own tempdir.
        video = VideoMetadata(id=video_id, source_url=url)
        dl_cfg = DownloadConfig(videos_dir=raw_dir)
        # silent=True routes yt-dlp's own ERROR lines into a null logger so
        # wrappers that scrape this script's stderr only see the rephrased
        # "download failed: ..." line emitted by the CLI itself.
        result = download_video(
            video,
            dl_cfg,
            fetch=partial(_yt_dlp_fetch, silent=True),
        )
        if not result.ok or result.local_path is None:
            raise DownloadError(result.error or "download_video returned no path")

        # 2. Duration probe + rejection.  ffprobe on the freshly-
        #    downloaded file rather than on the URL — TikTok URLs don't
        #    expose duration metadata before download anyway.
        duration_s = _ffprobe_duration(result.local_path)
        _log.info("video duration: %.2fs", duration_s)
        if max_duration_sec is not None and duration_s > max_duration_sec:
            raise _DurationRejected(duration_s, max_duration_sec)

        # 3. Preprocess (224x224 @ 8 fps, 16 kHz mono — defaults
        #    pinned by neural_media_inference.runner).
        pp_cfg = PreprocessConfig(processed_dir=proc_dir)
        pp = preprocess_video(result.local_path, video_id, pp_cfg)
        _log.info("preprocessed → %s (skipped=%s)", pp.local_path, pp.skipped)

        # 4. Infer.  Both backends honour the same signature; TribeBackend
        #    resolves the file via {videos_dir}/{video_id}.mp4 — that's
        #    `proc_dir / f"{video_id}.mp4"`, which is exactly what
        #    preprocess_video just wrote.
        backend = _build_backend(mock=mock, videos_dir=proc_dir)
        seed = _seed_from_url(url)
        activations = backend.infer(
            video_id=video_id,
            duration_s=duration_s,
            seed=seed,
            sample_rate_hz=SAMPLE_RATE_HZ,
        )

        # 5. Aggregate per-vertex → per-region timeseries.
        rows = aggregate_region_metrics(
            activations,
            video_id=video_id,
            inference_run_id=video_id,  # local; never persisted
        )
        by_region = {row["region_id"]: row["timeseries"] for row in rows}

        # 6. Wire payload.  Field names + order match
        #    services/hf-space/app.py:_run_job → activation_payload.
        return {
            "videoDurationSec": float(duration_s),
            "timestamps": _build_timestamps(int(activations.shape[0])),
            "byRegion": by_region,
            "modelVersion": str(backend.model_version),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class _DurationRejected(Exception):
    """Internal sentinel — the CLI maps this to exit code 2."""

    def __init__(self, duration_s: float, max_s: float) -> None:
        super().__init__(f"video is {duration_s:.1f}s; max accepted is {max_s:.1f}s")
        self.duration_s = duration_s
        self.max_s = max_s


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="predict_one_url.py",
        description="Predict brain activations for one video URL.  Emits JSON "
                    "matching the HF Space callback payload.",
    )
    p.add_argument("url", help="TikTok / video URL")
    p.add_argument(
        "--output", "-o", type=Path, default=None,
        help="write JSON to this path (default: stdout, pretty-printed)",
    )
    p.add_argument(
        "--mock", action="store_true",
        help="use MockBackend (no torch, no GPU, deterministic, sub-second)",
    )
    p.add_argument(
        "--max-duration-sec", type=float, default=None,
        help="reject videos longer than this many seconds (default: unlimited)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="log progress to stderr",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    try:
        payload = _run_pipeline(
            args.url,
            mock=args.mock,
            max_duration_sec=args.max_duration_sec,
        )
    except _DurationRejected as e:
        print(str(e), file=sys.stderr)
        return 2
    except DownloadError as e:
        if _looks_tiktok_blocked(e):
            print("tiktok_blocked", file=sys.stderr)
        print(f"download failed: {e}", file=sys.stderr)
        return 1
    except PreprocessError as e:
        print(f"inference failed (preprocess): {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        # Anything past download/preprocess is "inference" from the
        # caller's POV — backend errors, ffprobe failures on the
        # downloaded file, aggregator validation, etc.
        print(f"inference failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    if args.output is None:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        # Compact on disk to match the HF Space's wire format (which
        # gzips with no whitespace).  Pretty-print is only the stdout
        # affordance for humans.
        args.output.write_text(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
