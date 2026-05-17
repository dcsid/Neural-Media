"""Neural Media data pipeline.

Importer + downloader + preprocessor + orchestrator for a single-user,
local-first TikTok watch-history pipeline. Nothing in this package emits
network telemetry, and outbound network access is confined to the
downloader (yt-dlp).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The shared contracts package lives at repo-root/shared. We add the repo
# root to sys.path so `from shared.schemas import ...` works regardless of
# how this package is installed. Mirrors the pattern in
# services/api/neural_media_api/schemas_re_export.py until the shared
# contracts are published as a proper installable package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from .downloader import (  # noqa: E402
    DownloadConfig,
    DownloadError,
    DownloadResult,
    download_batch,
    download_video,
)
from .importer import parse_export, stable_video_id, stable_watch_event_id  # noqa: E402
from .preprocess import (  # noqa: E402
    PreprocessConfig,
    PreprocessError,
    PreprocessResult,
    preprocess_video,
)

__all__ = [
    "DownloadConfig",
    "DownloadError",
    "DownloadResult",
    "PreprocessConfig",
    "PreprocessError",
    "PreprocessResult",
    "download_batch",
    "download_video",
    "parse_export",
    "preprocess_video",
    "stable_video_id",
    "stable_watch_event_id",
]
