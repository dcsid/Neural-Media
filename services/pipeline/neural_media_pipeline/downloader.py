"""yt-dlp wrapper for the Neural Media pipeline.

TikTok aggressively rate-limits, so every network call goes through one
thin seam (``_yt_dlp_fetch``) with:

  * full-jitter exponential backoff, capped at 5 attempts
  * a rotating pool of public User-Agent strings
  * jittered inter-request spacing in batch mode
  * an on-disk dedup cache — if ``data/videos/{video_id}.mp4`` already
    exists, the network is not touched

Tests must NEVER hit TikTok. They monkeypatch ``_yt_dlp_fetch`` (or pass
a fake ``fetch=`` callable to ``download_video``/``download_batch``) and
inject deterministic ``sleep``/``rng`` shims.

Privacy: this module never logs full source URLs. The stable video id
is a hash of the URL and is safe to log; the URL itself is not. See
``docs/scientific-framing.md``.

URL forms supported by the wrapper
----------------------------------

The newer TikTok export (``Watch History.txt``) emits share-shortlink
URLs of the form::

    https://www.tiktokv.com/share/video/<numeric_id>/

rather than the older ``https://www.tiktok.com/@<handle>/video/<id>``
form. ``yt_dlp`` resolves the share host through a redirect to the
canonical tiktok.com URL and downloads the underlying mp4 the same way,
so no code change is needed here.

Probe recorded on 2026-05-17 against
``https://www.tiktokv.com/share/video/7640163791312801054/`` from the
user's local ``Watch History.txt`` (yt_dlp 2026.3.17, default
``_yt_dlp_fetch`` opts): OK, 3 752 713 bytes mp4 written. Re-run
``services/pipeline/scripts/probe_share_url.py`` (one network call) to
re-verify if yt-dlp ships an extractor change.
"""

from __future__ import annotations

import importlib.util
import logging
import random
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from shared.schemas import VideoMetadata

_log = logging.getLogger(__name__)

# Public, recent stable User-Agent strings. Kept short on purpose — too
# many uncommon UAs trips rate-limiting harder than a small rotation.
_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
)


class DownloadError(RuntimeError):
    """Raised after the retry budget for a single video is exhausted.

    The wrapped ``__cause__`` carries the last underlying failure.
    """


# CONTRACTS.md §13: a v2 job analyzes only the [startSec, endSec) window,
# capped at 90 s (no auto-trim — over-long segments are rejected, §13.5).
MAX_SEGMENT_SEC = 90.0


class SegmentError(ValueError):
    """Invalid ``[start_sec, end_sec)`` segment request.

    ``code`` is the CONTRACTS.md §13.2 ``error_code`` — ``bad_segment`` or
    ``segment_too_long`` — so a caller can surface it verbatim. The
    URL-shape check (``invalid_url``) and the duration-dependent
    ``segment_out_of_bounds`` check live with the caller that knows the
    real video length; these two are the request-only checks.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_segment(start_sec: float, end_sec: float) -> tuple[float, float]:
    """Validate a ``[start_sec, end_sec)`` window against CONTRACTS.md §13.2.

    Returns the pair unchanged on success; raises :class:`SegmentError`
    (carrying the contract ``error_code``) otherwise. Request-only — never
    downloads or probes the video.
    """
    if start_sec < 0 or start_sec >= end_sec:
        raise SegmentError(
            "bad_segment",
            f"segment must satisfy 0 <= startSec < endSec; got [{start_sec}, {end_sec})",
        )
    if end_sec - start_sec > MAX_SEGMENT_SEC:
        raise SegmentError(
            "segment_too_long",
            f"segment is {end_sec - start_sec:.3f}s; the cap is {MAX_SEGMENT_SEC:.0f}s",
        )
    return start_sec, end_sec


# Signature any fetch implementation (real or mock) must match. The base
# call is ``fetch(url, dest, user_agent)``; ``download_video`` additionally
# passes ``segment=(start, end)`` when a window is requested, so a fetcher
# that supports segment downloads accepts an optional ``segment`` keyword.
FetchFn = Callable[..., None]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class DownloadConfig:
    """Knobs for the downloader. All times in seconds."""

    videos_dir: Path
    max_attempts: int = 5
    base_backoff_s: float = 1.5
    max_backoff_s: float = 60.0
    inter_request_min_s: float = 0.8
    inter_request_max_s: float = 3.5
    user_agents: tuple[str, ...] = _USER_AGENTS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not self.user_agents:
            raise ValueError("user_agents must be non-empty")


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of one ``download_video`` call.

    ``skipped`` is True when the file already existed (cache hit) and
    no network call was made. ``error`` is None on success.
    """

    video_id: str
    local_path: Path | None
    skipped: bool
    attempts: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.local_path is not None


def _local_path(cfg: DownloadConfig, video_id: str) -> Path:
    return cfg.videos_dir / f"{video_id}.mp4"


def yt_dlp_available() -> bool:
    """True iff yt-dlp is importable as a Python module OR on PATH.

    Consumed by the api worker's ``/capabilities`` endpoint. The
    importable check is the operative one (``_yt_dlp_fetch`` does
    ``import yt_dlp``), but we accept the CLI as a fallback so a
    pip-uninstalled environment with the Homebrew binary still reports
    truthfully. ``importlib.util.find_spec`` avoids actually loading the
    package, so this stays cheap.
    """
    if importlib.util.find_spec("yt_dlp") is not None:
        return True
    return shutil.which("yt-dlp") is not None


class _NullYtdlLogger:
    """yt-dlp logger that drops every message on the floor.

    Even with ``quiet=True`` and ``no_warnings=True``, yt-dlp writes
    ``ERROR: ...`` lines directly to stderr when a fetch fails. Passing
    a logger= into ydl_opts is the only documented way to fully redirect
    that output. The logger protocol yt-dlp expects is just the four
    methods below.
    """

    def debug(self, _msg: str) -> None:  # noqa: D401 — yt-dlp protocol
        pass

    def info(self, _msg: str) -> None:
        pass

    def warning(self, _msg: str) -> None:
        pass

    def error(self, _msg: str) -> None:
        pass


def _yt_dlp_fetch(
    url: str,
    dest: Path,
    user_agent: str,
    *,
    silent: bool = False,
    segment: tuple[float, float] | None = None,
) -> None:
    """Single network seam. Lazily imports yt-dlp so test environments
    that monkeypatch this never need yt-dlp installed at all.

    On success ``dest`` exists as an .mp4 file. yt-dlp's own retry is
    disabled — we own retries at the call site.

    ``silent`` is an opt-in escape valve for wrappers (notably
    ``scripts/predict_one_url.py``) that scrape stderr and don't want
    yt-dlp's ERROR lines mixed in with their own rephrased failure
    messages. The pipeline orchestrator never sets it, so the
    long-standing batch-ingest log output is preserved.

    ``segment`` (``(start_sec, end_sec)``) restricts the download to that
    window via yt-dlp's ``download_ranges`` — the Python-API form of the
    ``--download-sections "*start-end"`` CLI flag — with
    ``force_keyframes_at_cuts`` so the cut is frame-accurate. ``None``
    downloads the whole video (CONTRACTS.md §13).
    """
    import yt_dlp  # type: ignore[import-not-found]  # lazy, optional dep

    dest.parent.mkdir(parents=True, exist_ok=True)
    # yt-dlp picks the extension based on the source container; we ask
    # for mp4 via format selection + merge container, then normalize the
    # filename below if it differs.
    out_template = str(dest.with_suffix("")) + ".%(ext)s"
    ydl_opts: dict[str, object] = {
        "outtmpl": out_template,
        "format": "mp4/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 0,
        "http_headers": {"User-Agent": user_agent},
        "concurrent_fragment_downloads": 1,
    }
    if silent:
        ydl_opts["logger"] = _NullYtdlLogger()
    if segment is not None:
        # Lazy import: download_range_func lives in yt_dlp.utils and is only
        # needed on the segment path.
        from yt_dlp.utils import download_range_func  # type: ignore[import-not-found]

        start_sec, end_sec = segment
        ydl_opts["download_ranges"] = download_range_func(None, [(start_sec, end_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    if dest.exists():
        return
    # yt-dlp produced a sibling with a different extension; rename it.
    for candidate in dest.parent.glob(f"{dest.stem}.*"):
        if candidate.is_file():
            candidate.rename(dest)
            return
    raise RuntimeError(f"yt-dlp produced no file at {dest.name}")


def _backoff_seconds(attempt: int, cfg: DownloadConfig, rng: random.Random) -> float:
    """Full-jitter exponential backoff (AWS architecture blog pattern).

    ``attempt`` is 1-indexed (we sleep AFTER attempt #1 fails, BEFORE
    attempt #2). The exponential cap is ``base * 2**(attempt-1)``,
    bounded by ``max_backoff_s``; the actual sleep is uniform in
    ``[0, cap]``.
    """
    cap = min(cfg.max_backoff_s, cfg.base_backoff_s * (2 ** (attempt - 1)))
    return rng.uniform(0.0, cap)


def download_video(
    video: VideoMetadata,
    cfg: DownloadConfig,
    *,
    fetch: FetchFn = _yt_dlp_fetch,
    sleep: SleepFn = time.sleep,
    rng: random.Random | None = None,
    segment: tuple[float, float] | None = None,
) -> DownloadResult:
    """Download one video with retry, backoff, and UA rotation.

    Idempotent: returns ``skipped=True`` and never touches the network
    if the destination file already exists with non-zero size.

    Raises ``DownloadError`` only after the retry budget is exhausted.
    Per-attempt failures are caught and retried.

    ``segment`` downloads only the ``[start_sec, end_sec)`` window
    (CONTRACTS.md §13). It is validated up front — a bad window raises
    :class:`SegmentError` before any network call (and is never retried) —
    and forwarded to the fetch seam. The on-disk cache key is the video id,
    so a caller mixing whole-video and segment fetches should use a
    distinct ``videos_dir`` per request (the bake tooling uses a tempdir).
    """
    if segment is not None:
        validate_segment(*segment)
    rng = rng if rng is not None else random.Random()
    dest = _local_path(cfg, video.id)

    if dest.exists() and dest.stat().st_size > 0:
        _log.debug("event=download_cache_hit video_id=%s", video.id)
        return DownloadResult(video_id=video.id, local_path=dest, skipped=True, attempts=0)

    # Only thread `segment` through when set, so fetch seams that take the
    # base (url, dest, ua) signature keep working for whole-video fetches.
    fetch_kwargs: dict[str, object] = {"segment": segment} if segment is not None else {}
    last_err: BaseException | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        ua = rng.choice(cfg.user_agents)
        try:
            fetch(video.source_url, dest, ua, **fetch_kwargs)
        except Exception as exc:  # noqa: BLE001 — yt-dlp raises a wide variety
            last_err = exc
            _log.debug(
                "event=download_attempt_failed video_id=%s attempt=%d max_attempts=%d",
                video.id, attempt, cfg.max_attempts,
            )
            if attempt == cfg.max_attempts:
                break
            sleep(_backoff_seconds(attempt, cfg, rng))
            continue

        _log.debug("event=download_complete video_id=%s attempts=%d", video.id, attempt)
        return DownloadResult(
            video_id=video.id, local_path=dest, skipped=False, attempts=attempt,
        )

    raise DownloadError(
        f"giving up on video {video.id} after {cfg.max_attempts} attempts"
    ) from last_err


def download_batch(
    videos: Iterable[VideoMetadata],
    cfg: DownloadConfig,
    *,
    fetch: FetchFn = _yt_dlp_fetch,
    sleep: SleepFn = time.sleep,
    rng: random.Random | None = None,
    stop_on_error: bool = False,
) -> list[DownloadResult]:
    """Download several videos with jittered inter-request spacing.

    Per-video failures are captured as ``DownloadResult(error=...)``
    rows by default; pass ``stop_on_error=True`` to re-raise instead.
    Cache hits do not consume a spacing delay — only real network calls
    do.
    """
    rng = rng if rng is not None else random.Random()
    results: list[DownloadResult] = []
    network_calls = 0

    for video in videos:
        dest = _local_path(cfg, video.id)
        cache_hit = dest.exists() and dest.stat().st_size > 0

        if not cache_hit and network_calls > 0:
            sleep(rng.uniform(cfg.inter_request_min_s, cfg.inter_request_max_s))

        try:
            res = download_video(video, cfg, fetch=fetch, sleep=sleep, rng=rng)
        except DownloadError as exc:
            if stop_on_error:
                raise
            # no URL in log — only the id (a hash of the URL) is safe to emit
            _log.warning(
                "event=download_failed video_id=%s attempts=%d",
                video.id, cfg.max_attempts,
            )
            results.append(
                DownloadResult(
                    video_id=video.id,
                    local_path=None,
                    skipped=False,
                    attempts=cfg.max_attempts,
                    error=str(exc),
                )
            )
            network_calls += 1
            continue

        results.append(res)
        if not res.skipped:
            network_calls += 1

    return results


__all__ = [
    "MAX_SEGMENT_SEC",
    "DownloadConfig",
    "DownloadError",
    "DownloadResult",
    "FetchFn",
    "SegmentError",
    "SleepFn",
    "download_batch",
    "download_video",
    "validate_segment",
    "yt_dlp_available",
]
