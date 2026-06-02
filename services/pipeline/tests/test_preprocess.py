"""Preprocessor tests — no subprocesses, no ffmpeg required.

Every call into ``preprocess_video`` injects a fake ``run`` callable
that writes a stub output file instead of invoking ffmpeg. The point
of these tests is to lock in:
  * the exact ffmpeg arg list
  * defaults pinned to ml-inference's ``DEFAULT_PREPROCESSING_PARAMS``
  * cache-hit idempotence
  * the ``extra_params`` payload the orchestrator passes to
    ``run_inference``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from neural_media_inference.runner import DEFAULT_PREPROCESSING_PARAMS

from neural_media_pipeline.preprocess import (
    PreprocessConfig,
    PreprocessError,
    build_ffmpeg_args,
    ffmpeg_available,
    preprocess_video,
)


def _cfg(tmp_path: Path, **overrides) -> PreprocessConfig:
    base = {"processed_dir": tmp_path / "processed"}
    base.update(overrides)
    return PreprocessConfig(**base)


def _make_src(tmp_path: Path, name: str = "abc-123.mp4") -> Path:
    p = tmp_path / "videos" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake mp4 bytes")
    return p


def _fake_run(write_to: Path) -> Callable[[list[str]], None]:
    """ffmpeg fake: extract the output path from argv and write a stub."""

    def _run(args: list[str]) -> None:
        out = Path(args[-1])  # build_ffmpeg_args places dest last
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"preprocessed")
        write_to.write_bytes(b"called")  # signal we ran

    return _run


# ---------------------------------------------------------------------------
# Defaults are pinned to ml-inference
# ---------------------------------------------------------------------------

def test_defaults_match_ml_inference(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert cfg.video_resolution == DEFAULT_PREPROCESSING_PARAMS["video_resolution"]
    assert cfg.video_fps == DEFAULT_PREPROCESSING_PARAMS["video_fps"]
    assert cfg.audio_sample_rate_hz == DEFAULT_PREPROCESSING_PARAMS["audio_sample_rate_hz"]


def test_extra_params_contract(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    params = cfg.extra_params()
    # Must include every key the inference runner's
    # DEFAULT_PREPROCESSING_PARAMS defines, so the merge stays consistent.
    for key in DEFAULT_PREPROCESSING_PARAMS:
        assert key in params
        assert params[key] == DEFAULT_PREPROCESSING_PARAMS[key]


# ---------------------------------------------------------------------------
# ffmpeg argv shape
# ---------------------------------------------------------------------------

def test_ffmpeg_args_capture_target(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    src = tmp_path / "in.mp4"
    dest = tmp_path / "out.mp4"
    args = build_ffmpeg_args(src, dest, cfg)

    # Resolution
    assert "-vf" in args
    assert args[args.index("-vf") + 1] == "scale=224:224:flags=bicubic"
    # FPS
    assert "-r" in args and args[args.index("-r") + 1] == "8"
    # Audio sample rate + channels
    assert "-ar" in args and args[args.index("-ar") + 1] == "16000"
    assert "-ac" in args and args[args.index("-ac") + 1] == "1"
    # Input + output positions
    assert args[args.index("-i") + 1] == str(src)
    assert args[-1] == str(dest)
    # Quiet ffmpeg
    assert "-loglevel" in args


def test_ffmpeg_args_respect_overrides(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        video_resolution="320x240",
        video_fps=16,
        audio_sample_rate_hz=22050,
        audio_channels=2,
    )
    args = build_ffmpeg_args(tmp_path / "i.mp4", tmp_path / "o.mp4", cfg)
    assert args[args.index("-vf") + 1] == "scale=320:240:flags=bicubic"
    assert args[args.index("-r") + 1] == "16"
    assert args[args.index("-ar") + 1] == "22050"
    assert args[args.index("-ac") + 1] == "2"


# ---------------------------------------------------------------------------
# preprocess_video behavior
# ---------------------------------------------------------------------------

def test_first_run_invokes_ffmpeg(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    src = _make_src(tmp_path)
    marker = tmp_path / "marker"

    res = preprocess_video(src, "abc-123", cfg, run=_fake_run(marker))

    assert res.skipped is False
    assert res.local_path.name == "abc-123.mp4"
    assert res.local_path.parent == cfg.processed_dir
    assert res.local_path.exists()
    assert marker.exists()


def test_cache_hit_skips_ffmpeg(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    src = _make_src(tmp_path)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    pre = cfg.processed_dir / "abc-123.mp4"
    pre.write_bytes(b"already done")

    def boom(args: list[str]) -> None:  # pragma: no cover
        raise AssertionError("ffmpeg must not run on cache hit")

    res = preprocess_video(src, "abc-123", cfg, run=boom)
    assert res.skipped is True
    assert res.local_path == pre


def test_missing_source_raises(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with pytest.raises(FileNotFoundError):
        preprocess_video(tmp_path / "nope.mp4", "vid", cfg, run=lambda args: None)


def test_directory_source_raises(tmp_path: Path) -> None:
    """A directory (exists() is True but is_file() is False) is rejected
    clearly before ffmpeg is ever invoked, rather than being handed to
    ffmpeg as an unreadable input."""
    cfg = _cfg(tmp_path)
    a_dir = tmp_path / "videos" / "looks-like.mp4"  # a dir named like a file
    a_dir.mkdir(parents=True)

    def boom(args: list[str]) -> None:  # pragma: no cover — must not run
        raise AssertionError("ffmpeg must not run on a non-file source")

    with pytest.raises(FileNotFoundError, match="not a regular file"):
        preprocess_video(a_dir, "vid", cfg, run=boom)


def test_ffmpeg_failure_propagates(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    src = _make_src(tmp_path)

    def fail(args: list[str]) -> None:
        raise PreprocessError("ffmpeg crashed")

    with pytest.raises(PreprocessError):
        preprocess_video(src, "abc-123", cfg, run=fail)


def test_silent_success_with_no_output_is_an_error(tmp_path: Path) -> None:
    """If ffmpeg returns 0 but writes nothing, we still surface it."""
    cfg = _cfg(tmp_path)
    src = _make_src(tmp_path)

    def silent(args: list[str]) -> None:
        return None  # noqa: PIE790

    with pytest.raises(PreprocessError):
        preprocess_video(src, "abc-123", cfg, run=silent)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_invalid_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PreprocessConfig(processed_dir=tmp_path, video_resolution="bad")
    with pytest.raises(ValueError):
        PreprocessConfig(processed_dir=tmp_path, video_resolution="0x224")
    with pytest.raises(ValueError):
        PreprocessConfig(processed_dir=tmp_path, video_fps=0)
    with pytest.raises(ValueError):
        PreprocessConfig(processed_dir=tmp_path, audio_sample_rate_hz=7000)
    with pytest.raises(ValueError):
        PreprocessConfig(processed_dir=tmp_path, audio_channels=3)


def test_resolution_wh_property(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, video_resolution="640x360")
    assert cfg.resolution_wh == (640, 360)


def test_resolution_with_three_dimensions_rejected(tmp_path: Path) -> None:
    """'224x224x10' must fail with a clear 'WxH' message, not a cryptic
    "too many values to unpack" error from the naive split('x')."""
    with pytest.raises(ValueError, match="WxH"):
        PreprocessConfig(processed_dir=tmp_path, video_resolution="224x224x10")


def test_resolution_with_non_integer_dimension_rejected(tmp_path: Path) -> None:
    """A non-numeric or missing dimension fails with a clear message."""
    with pytest.raises(ValueError, match="integer"):
        PreprocessConfig(processed_dir=tmp_path, video_resolution="224xtall")
    with pytest.raises(ValueError, match="integer"):
        PreprocessConfig(processed_dir=tmp_path, video_resolution="224x")


# ---------------------------------------------------------------------------
# Precheck: ffmpeg_available
# ---------------------------------------------------------------------------

def test_ffmpeg_available_true_when_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neural_media_pipeline.preprocess as pp

    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: "/usr/local/bin/ffmpeg" if name == "ffmpeg" else None)
    assert ffmpeg_available() is True


def test_ffmpeg_available_false_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neural_media_pipeline.preprocess as pp

    monkeypatch.setattr(pp.shutil, "which", lambda _name: None)
    assert ffmpeg_available() is False
