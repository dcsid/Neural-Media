"""Downloader tests.

These tests MUST NOT reach TikTok. Every call into ``download_video`` /
``download_batch`` injects a fake ``fetch`` callable; the real
``_yt_dlp_fetch`` is never invoked. ``sleep`` is also injected so the
suite runs in milliseconds.
"""

from __future__ import annotations

import logging
import random
import sys
from pathlib import Path

import pytest

from neural_media_pipeline.downloader import (
    DownloadConfig,
    DownloadError,
    download_batch,
    download_video,
)
from shared.schemas import VideoMetadata


def _video(vid: str = "abc-123") -> VideoMetadata:
    return VideoMetadata(
        id=vid,
        source_url=f"https://www.tiktok.com/@x/video/{vid}",
        author="x",
    )


def _cfg(tmp_path: Path, **overrides) -> DownloadConfig:
    base = {
        "videos_dir": tmp_path / "videos",
        "max_attempts": 5,
        "base_backoff_s": 0.1,
        "max_backoff_s": 1.0,
        "inter_request_min_s": 0.05,
        "inter_request_max_s": 0.2,
    }
    base.update(overrides)
    return DownloadConfig(**base)


class _Sleeps:
    """Captures sleep durations without actually sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ---------------------------------------------------------------------------
# Happy-path / dedup
# ---------------------------------------------------------------------------

def test_first_attempt_success(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    calls: list[tuple[str, Path, str]] = []

    def fake_fetch(url: str, dest: Path, ua: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE_MP4")
        calls.append((url, dest, ua))

    sleeps = _Sleeps()
    res = download_video(_video(), cfg, fetch=fake_fetch, sleep=sleeps, rng=random.Random(0))

    assert res.ok is True
    assert res.skipped is False
    assert res.attempts == 1
    assert res.local_path is not None and res.local_path.name == "abc-123.mp4"
    assert res.local_path.exists()
    assert len(calls) == 1
    assert sleeps.calls == []  # no retry sleeps on first-attempt success
    assert calls[0][0].endswith("/abc-123")


def test_cache_hit_skips_network(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    (cfg.videos_dir).mkdir(parents=True, exist_ok=True)
    pre = cfg.videos_dir / "abc-123.mp4"
    pre.write_bytes(b"already downloaded")

    def boom(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("fetch must not be called on cache hit")

    res = download_video(_video(), cfg, fetch=boom, sleep=_Sleeps())
    assert res.skipped is True
    assert res.ok is True
    assert res.attempts == 0


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------

def test_retries_then_succeeds(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    attempts: list[int] = []

    def flaky(url: str, dest: Path, ua: str) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("rate-limited")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    sleeps = _Sleeps()
    res = download_video(_video(), cfg, fetch=flaky, sleep=sleeps, rng=random.Random(42))

    assert res.ok and res.attempts == 3
    assert len(attempts) == 3
    # Two failed attempts → two backoff sleeps.
    assert len(sleeps.calls) == 2


def test_retry_exhausted_raises(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, max_attempts=3)

    def always_fails(url: str, dest: Path, ua: str) -> None:
        raise RuntimeError("nope")

    sleeps = _Sleeps()
    with pytest.raises(DownloadError) as exc_info:
        download_video(
            _video(),
            cfg,
            fetch=always_fails,
            sleep=sleeps,
            rng=random.Random(0),
        )

    assert "after 3 attempts" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    # 3 attempts → 2 inter-attempt sleeps (none after the final failure).
    assert len(sleeps.calls) == 2


def test_backoff_respects_cap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, base_backoff_s=10.0, max_backoff_s=2.0, max_attempts=5)

    def always_fails(*args, **kwargs) -> None:
        raise RuntimeError("nope")

    sleeps = _Sleeps()
    with pytest.raises(DownloadError):
        download_video(_video(), cfg, fetch=always_fails, sleep=sleeps, rng=random.Random(1))

    assert all(0.0 <= s <= cfg.max_backoff_s for s in sleeps.calls)


# ---------------------------------------------------------------------------
# User-Agent rotation
# ---------------------------------------------------------------------------

def test_user_agent_rotates_across_retries(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        max_attempts=5,
        user_agents=("UA-A", "UA-B", "UA-C"),
    )
    seen: list[str] = []

    def fail_then_succeed(url: str, dest: Path, ua: str) -> None:
        seen.append(ua)
        if len(seen) < 4:
            raise RuntimeError("rate limit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    download_video(
        _video(),
        cfg,
        fetch=fail_then_succeed,
        sleep=_Sleeps(),
        rng=random.Random(7),
    )

    # We exercised four attempts; expect more than one distinct UA picked.
    assert len(seen) == 4
    assert len(set(seen)) >= 2
    assert all(ua in cfg.user_agents for ua in seen)


# ---------------------------------------------------------------------------
# Batch behavior
# ---------------------------------------------------------------------------

def test_batch_spaces_real_calls_but_not_cache_hits(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    (cfg.videos_dir).mkdir(parents=True, exist_ok=True)
    # Pre-populate vid-2 as a cache hit.
    (cfg.videos_dir / "vid-2.mp4").write_bytes(b"cached")

    def fake_fetch(url: str, dest: Path, ua: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    sleeps = _Sleeps()
    vids = [_video("vid-1"), _video("vid-2"), _video("vid-3"), _video("vid-4")]
    results = download_batch(vids, cfg, fetch=fake_fetch, sleep=sleeps, rng=random.Random(3))

    assert [r.video_id for r in results] == ["vid-1", "vid-2", "vid-3", "vid-4"]
    assert [r.skipped for r in results] == [False, True, False, False]
    # Three real network calls → spacings happen before #3 (after #1) and
    # before #4 (after #3). The cache hit at #2 contributes nothing.
    spacing_sleeps = [s for s in sleeps.calls if cfg.inter_request_min_s <= s <= cfg.inter_request_max_s]
    assert len(spacing_sleeps) == 2


def test_batch_captures_per_video_failure_by_default(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, max_attempts=2)

    def fetch(url: str, dest: Path, ua: str) -> None:
        if "vid-2" in url:
            raise RuntimeError("doomed")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    sleeps = _Sleeps()
    results = download_batch(
        [_video("vid-1"), _video("vid-2"), _video("vid-3")],
        cfg,
        fetch=fetch,
        sleep=sleeps,
        rng=random.Random(0),
    )
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].error and "vid-2" in results[1].error


def test_batch_stop_on_error_propagates(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, max_attempts=2)

    def fetch(url: str, dest: Path, ua: str) -> None:
        raise RuntimeError("bad")

    with pytest.raises(DownloadError):
        download_batch(
            [_video("vid-1"), _video("vid-2")],
            cfg,
            fetch=fetch,
            sleep=_Sleeps(),
            rng=random.Random(0),
            stop_on_error=True,
        )


# ---------------------------------------------------------------------------
# Privacy + integration hygiene
# ---------------------------------------------------------------------------

def test_no_full_url_in_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = _cfg(tmp_path, max_attempts=2)

    def fetch(url: str, dest: Path, ua: str) -> None:
        raise RuntimeError("nope")

    caplog.set_level(logging.DEBUG, logger="neural_media_pipeline.downloader")
    with pytest.raises(DownloadError):
        download_video(_video("vid-secret"), cfg, fetch=fetch, sleep=_Sleeps())

    full_url = "https://www.tiktok.com/@x/video/vid-secret"
    assert all(full_url not in rec.getMessage() for rec in caplog.records)


def test_default_path_does_not_import_yt_dlp(tmp_path: Path) -> None:
    # Sanity: with the default fetch never invoked, yt-dlp must not load.
    sys.modules.pop("yt_dlp", None)
    cfg = _cfg(tmp_path)

    def fake_fetch(url: str, dest: Path, ua: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    download_video(_video(), cfg, fetch=fake_fetch, sleep=_Sleeps())
    assert "yt_dlp" not in sys.modules


def test_invalid_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DownloadConfig(videos_dir=tmp_path, max_attempts=0)
    with pytest.raises(ValueError):
        DownloadConfig(videos_dir=tmp_path, user_agents=())
