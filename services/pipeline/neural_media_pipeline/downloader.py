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
"""

from __future__ import annotations

import logging
import random
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


# Signature any fetch implementation (real or mock) must match.
FetchFn = Callable[[str, Path, str], None]
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


def _yt_dlp_fetch(url: str, dest: Path, user_agent: str) -> None:
    """Single network seam. Lazily imports yt-dlp so test environments
    that monkeypatch this never need yt-dlp installed at all.

    On success ``dest`` exists as an .mp4 file. yt-dlp's own retry is
    disabled — we own retries at the call site.
    """
    import yt_dlp  # type: ignore[import-not-found]  # lazy, optional dep

    dest.parent.mkdir(parents=True, exist_ok=True)
    # yt-dlp picks the extension based on the source container; we ask
    # for mp4 via format selection + merge container, then normalize the
    # filename below if it differs.
    out_template = str(dest.with_suffix("")) + ".%(ext)s"
    ydl_opts = {
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
) -> DownloadResult:
    """Download one video with retry, backoff, and UA rotation.

    Idempotent: returns ``skipped=True`` and never touches the network
    if the destination file already exists with non-zero size.

    Raises ``DownloadError`` only after the retry budget is exhausted.
    Per-attempt failures are caught and retried.
    """
    rng = rng if rng is not None else random.Random()
    dest = _local_path(cfg, video.id)

    if dest.exists() and dest.stat().st_size > 0:
        _log.debug("cache hit for %s", video.id)
        return DownloadResult(video_id=video.id, local_path=dest, skipped=True, attempts=0)

    last_err: BaseException | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        ua = rng.choice(cfg.user_agents)
        try:
            fetch(video.source_url, dest, ua)
        except Exception as exc:  # noqa: BLE001 — yt-dlp raises a wide variety
            last_err = exc
            _log.debug("attempt %d/%d failed for %s", attempt, cfg.max_attempts, video.id)
            if attempt == cfg.max_attempts:
                break
            sleep(_backoff_seconds(attempt, cfg, rng))
            continue

        _log.debug("downloaded %s in %d attempt(s)", video.id, attempt)
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
            _log.warning("download failed for %s", video.id)  # no URL in log
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
    "DownloadConfig",
    "DownloadError",
    "DownloadResult",
    "FetchFn",
    "SleepFn",
    "download_batch",
    "download_video",
]
