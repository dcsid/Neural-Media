"""ffmpeg-based normalization to the TRIBE v2 input shape.

Target resolution / FPS / audio sample rate are NOT defined here.
``PreprocessConfig`` pulls its defaults from
``neural_media_inference.runner.DEFAULT_PREPROCESSING_PARAMS`` so the
data-pipeline literally cannot drift from the values TRIBE expects.
The same dict is echoed into ``run_inference``'s ``extra_params`` by
the orchestrator so it lands in the reproducibility envelope
(CONTRACTS.md §8).

All ffmpeg invocations go through one seam (``_ffmpeg_run``); tests
inject a fake to avoid spawning subprocesses or requiring ffmpeg on
the runner.

Privacy: this module deals only with local file paths derived from
video ids (hashes of the URL), never URLs.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Canonical TRIBE v2 input target — owned by ml-inference. We import
# rather than copy to make drift physically impossible.
from neural_media_inference.runner import DEFAULT_PREPROCESSING_PARAMS

_log = logging.getLogger(__name__)


class PreprocessError(RuntimeError):
    """ffmpeg exited non-zero or produced no output."""


# Test seam: any callable matching this signature can replace _ffmpeg_run.
# Implementations receive only the ffmpeg ARG list (no binary path), and
# are responsible for raising PreprocessError on failure.
RunFn = Callable[[list[str]], None]


def ffmpeg_available() -> bool:
    """True iff a usable ``ffmpeg`` binary is on PATH.

    Consumed by the api worker's ``/capabilities`` endpoint so the
    frontend can degrade the import flow cleanly when the binary is
    missing (e.g. CI runners or a fresh laptop without ``brew install
    ffmpeg``). Cheap — ``shutil.which`` is a single PATH walk and does
    not exec anything.
    """
    return shutil.which("ffmpeg") is not None


def _ffmpeg_run(args: list[str]) -> None:
    """Run system ffmpeg with the given argv tail. Raises on non-zero.

    Resolves ffmpeg on PATH at call-time so a missing binary surfaces a
    clear error instead of a generic ``FileNotFoundError``.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise PreprocessError("ffmpeg not found on PATH; install via `brew install ffmpeg`")
    completed = subprocess.run([ffmpeg, *args], check=False, capture_output=True)
    if completed.returncode != 0:
        # ffmpeg is chatty; keep the tail so the error message stays small.
        tail = completed.stderr[-400:].decode("utf-8", errors="replace")
        raise PreprocessError(f"ffmpeg exited {completed.returncode}: ...{tail}")


@dataclass(frozen=True)
class PreprocessConfig:
    """Normalization target.

    The first three fields default to the values pinned in
    ``neural_media_inference.runner.DEFAULT_PREPROCESSING_PARAMS``.
    Changing them here is meaningless unless ml-inference changes too —
    coordinate via terminal 1.
    """

    processed_dir: Path
    video_resolution: str = str(DEFAULT_PREPROCESSING_PARAMS["video_resolution"])
    video_fps: int = int(DEFAULT_PREPROCESSING_PARAMS["video_fps"])
    audio_sample_rate_hz: int = int(DEFAULT_PREPROCESSING_PARAMS["audio_sample_rate_hz"])
    audio_channels: int = 1
    video_codec: str = "libx264"
    video_preset: str = "veryfast"
    video_crf: int = 23
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"

    def __post_init__(self) -> None:
        if "x" not in self.video_resolution.lower():
            raise ValueError("video_resolution must be 'WxH', e.g. '224x224'")
        w, h = self.resolution_wh
        if w <= 0 or h <= 0:
            raise ValueError("video_resolution dimensions must be positive")
        if self.video_fps < 1:
            raise ValueError("video_fps must be >= 1")
        if self.audio_sample_rate_hz < 8000:
            raise ValueError("audio_sample_rate_hz must be >= 8000")
        if self.audio_channels not in (1, 2):
            raise ValueError("audio_channels must be 1 or 2")

    @property
    def resolution_wh(self) -> tuple[int, int]:
        w, h = self.video_resolution.lower().split("x")
        return int(w), int(h)

    def extra_params(self) -> dict[str, Any]:
        """Payload for ``run_inference(extra_params=...)``.

        Keys MUST overlap with ``DEFAULT_PREPROCESSING_PARAMS`` so the
        merge in the runner produces a consistent reproducibility
        envelope.
        """
        return {
            "video_resolution": self.video_resolution,
            "video_fps": self.video_fps,
            "audio_sample_rate_hz": self.audio_sample_rate_hz,
            "audio_channels": self.audio_channels,
            "video_codec": self.video_codec,
            "video_preset": self.video_preset,
            "video_crf": self.video_crf,
            "audio_codec": self.audio_codec,
            "audio_bitrate": self.audio_bitrate,
        }


@dataclass(frozen=True)
class PreprocessResult:
    video_id: str
    local_path: Path
    skipped: bool


def _output_path(cfg: PreprocessConfig, video_id: str) -> Path:
    return cfg.processed_dir / f"{video_id}.mp4"


def build_ffmpeg_args(src: Path, dest: Path, cfg: PreprocessConfig) -> list[str]:
    """Return the ffmpeg argv tail (no leading binary path).

    Exposed for tests so the exact invocation is locked in. ``-y``
    overwrites a partial output; the caller still gates re-runs with the
    on-disk cache check in ``preprocess_video``.
    """
    w, h = cfg.resolution_wh
    return [
        "-y",
        "-loglevel", "error",
        "-i", str(src),
        "-vf", f"scale={w}:{h}:flags=bicubic",
        "-r", str(cfg.video_fps),
        "-c:v", cfg.video_codec,
        "-preset", cfg.video_preset,
        "-crf", str(cfg.video_crf),
        "-ar", str(cfg.audio_sample_rate_hz),
        "-ac", str(cfg.audio_channels),
        "-c:a", cfg.audio_codec,
        "-b:a", cfg.audio_bitrate,
        "-movflags", "+faststart",
        str(dest),
    ]


def preprocess_video(
    src: Path,
    video_id: str,
    cfg: PreprocessConfig,
    *,
    run: RunFn = _ffmpeg_run,
) -> PreprocessResult:
    """Normalize one downloaded video into TRIBE v2's input shape.

    Idempotent: if the processed output already exists with non-zero
    size, returns ``skipped=True`` and does not invoke ffmpeg.
    """
    if not src.exists():
        raise FileNotFoundError(f"source video missing: {src.name}")

    dest = _output_path(cfg, video_id)
    if dest.exists() and dest.stat().st_size > 0:
        _log.debug("preprocess cache hit for %s", video_id)
        return PreprocessResult(video_id=video_id, local_path=dest, skipped=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    run(build_ffmpeg_args(src, dest, cfg))

    if not dest.exists() or dest.stat().st_size == 0:
        raise PreprocessError(f"preprocess produced no output for {video_id}")
    _log.debug("preprocessed %s", video_id)
    return PreprocessResult(video_id=video_id, local_path=dest, skipped=False)


__all__ = [
    "PreprocessConfig",
    "PreprocessError",
    "PreprocessResult",
    "RunFn",
    "build_ffmpeg_args",
    "ffmpeg_available",
    "preprocess_video",
]
