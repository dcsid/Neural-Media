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
    MAX_SEGMENT_SEC,
    DownloadConfig,
    DownloadError,
    SegmentError,
    download_batch,
    download_video,
    validate_segment,
    yt_dlp_available,
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


def test_zero_byte_cached_file_is_redownloaded(tmp_path: Path) -> None:
    """A leftover zero-byte file (e.g. an interrupted prior download) is NOT
    a valid cache hit — it must be re-fetched rather than skipped, otherwise
    the pipeline would carry an empty mp4 forward forever."""
    cfg = _cfg(tmp_path)
    cfg.videos_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.videos_dir / "abc-123.mp4"
    dest.write_bytes(b"")  # zero bytes

    def fake_fetch(url: str, dest: Path, ua: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"REAL_MP4_BYTES")

    res = download_video(
        _video(), cfg, fetch=fake_fetch, sleep=_Sleeps(), rng=random.Random(0),
    )
    assert res.skipped is False  # zero-byte file is not treated as cached
    assert res.ok is True
    assert res.attempts == 1
    assert dest.read_bytes() == b"REAL_MP4_BYTES"


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


# ---------------------------------------------------------------------------
# Precheck: yt_dlp_available
# ---------------------------------------------------------------------------

def test_yt_dlp_available_returns_true_when_module_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "yt_dlp" else None,
    )
    assert yt_dlp_available() is True


def test_yt_dlp_available_falls_back_to_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    import neural_media_pipeline.downloader as dl

    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(dl.shutil, "which",
                        lambda name: "/usr/local/bin/yt-dlp" if name == "yt-dlp" else None)
    assert yt_dlp_available() is True


def test_yt_dlp_available_returns_false_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    import neural_media_pipeline.downloader as dl

    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(dl.shutil, "which", lambda _name: None)
    assert yt_dlp_available() is False


# ---------------------------------------------------------------------------
# Segment download (--download-sections) — CONTRACTS.md §13
# ---------------------------------------------------------------------------

def test_validate_segment_accepts_valid() -> None:
    assert validate_segment(10.0, 25.0) == (10.0, 25.0)
    assert validate_segment(0.0, MAX_SEGMENT_SEC) == (0.0, MAX_SEGMENT_SEC)


@pytest.mark.parametrize("start,end", [(-1.0, 10.0), (10.0, 10.0), (20.0, 5.0)])
def test_validate_segment_bad_segment(start: float, end: float) -> None:
    with pytest.raises(SegmentError) as exc_info:
        validate_segment(start, end)
    assert exc_info.value.code == "bad_segment"


def test_validate_segment_too_long() -> None:
    with pytest.raises(SegmentError) as exc_info:
        validate_segment(0.0, MAX_SEGMENT_SEC + 0.1)
    assert exc_info.value.code == "segment_too_long"


def test_download_video_forwards_segment_to_fetch(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    captured: dict[str, object] = {}

    def fake_fetch(url: str, dest: Path, ua: str, *, segment=None) -> None:
        captured["segment"] = segment
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"seg")

    res = download_video(
        _video(), cfg, fetch=fake_fetch, sleep=_Sleeps(),
        rng=random.Random(0), segment=(12.0, 30.0),
    )
    assert res.ok is True and res.skipped is False
    assert captured["segment"] == (12.0, 30.0)


def test_download_video_without_segment_uses_base_fetch_signature(tmp_path: Path) -> None:
    """A fetcher taking only (url, dest, ua) still works when no segment is
    requested — the segment kwarg is only threaded through when set."""
    cfg = _cfg(tmp_path)

    def base_fetch(url: str, dest: Path, ua: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"ok")

    res = download_video(_video(), cfg, fetch=base_fetch, sleep=_Sleeps(), rng=random.Random(0))
    assert res.ok is True and res.skipped is False


def test_download_video_invalid_segment_raises_before_fetch(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    def boom_fetch(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("fetch must not run on an invalid segment")

    with pytest.raises(SegmentError) as exc_info:
        download_video(_video(), cfg, fetch=boom_fetch, sleep=_Sleeps(), segment=(50.0, 10.0))
    assert exc_info.value.code == "bad_segment"


def test_yt_dlp_fetch_wires_download_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_yt_dlp_fetch`` with a segment passes ``download_ranges`` +
    ``force_keyframes_at_cuts`` to yt-dlp — verified via a fake yt_dlp
    module so the test needs no real yt-dlp install."""
    import types

    from neural_media_pipeline.downloader import _yt_dlp_fetch

    dest = tmp_path / "vid.mp4"
    captured: dict[str, object] = {}

    class _FakeYDL:
        def __init__(self, opts):
            captured["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def download(self, _urls):
            dest.write_bytes(b"seg")  # simulate yt-dlp writing the file

    fake_yt = types.ModuleType("yt_dlp")
    fake_yt.YoutubeDL = _FakeYDL  # type: ignore[attr-defined]
    fake_utils = types.ModuleType("yt_dlp.utils")
    fake_utils.download_range_func = lambda chapters, ranges: ("RANGES", ranges)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_yt)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", fake_utils)

    _yt_dlp_fetch("https://www.youtube.com/watch?v=abc", dest, "UA", segment=(12.0, 30.0))

    opts = captured["opts"]
    assert opts["force_keyframes_at_cuts"] is True
    assert opts["download_ranges"] == ("RANGES", [(12.0, 30.0)])
