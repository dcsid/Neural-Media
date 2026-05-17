"""TikTok export → VideoMetadata + WatchEvent rows.

TikTok's "Download your data" flow yields either a raw ``user_data.json``
file or a ``.zip`` archive containing that file (the wrapper has changed
across product revisions). ``parse_export`` accepts either form. For
zips we locate ``user_data.json`` case-insensitively at any depth and
read it directly from the archive — nothing is extracted to disk.

Browsing history lives at ``Activity > Video Browsing History > VideoList``
and is the only section this importer consumes. Other top-level keys
(Profile, Favorite Videos, Like List, ...) are ignored.

The export shape drifts between TikTok releases, so this parser is
deliberately tolerant: missing optional fields are skipped silently, and
entries missing the two required fields (``Link`` and ``Date``) are
dropped with a debug log. Pair every parser change with an update to
``data/sample/tiktok_export/user_data.json``.

Privacy: this module never logs full URLs. The stable video id is a hash
of the URL and is safe to log; the URL itself is not. ``data/videos/`` is
gitignored — see ``docs/scientific-framing.md``.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.schemas import VideoMetadata, WatchEvent

# Fixed UUID5 namespaces. Treat these as part of the on-disk contract —
# changing them invalidates every previously-stored id.
_NAMESPACE_VIDEO = uuid.UUID("3a6b1c4f-7d2e-4a8b-9c0d-1e2f3a4b5c6d")
_NAMESPACE_EVENT = uuid.UUID("8f1d2c3b-4a5e-4f7d-8e9a-0b1c2d3e4f50")

# Matches `/@handle/` in a TikTok video URL.
_AUTHOR_RE = re.compile(r"/@([^/]+)/")

_log = logging.getLogger(__name__)


def stable_video_id(source_url: str) -> str:
    """Deterministic id for a video URL. Same URL → same id, forever."""
    return str(uuid.uuid5(_NAMESPACE_VIDEO, source_url))


def stable_watch_event_id(video_id: str, watched_at: datetime) -> str:
    """Deterministic id for a (video, timestamp) playback occurrence."""
    return str(uuid.uuid5(_NAMESPACE_EVENT, f"{video_id}|{watched_at.isoformat()}"))


def _parse_date(raw: str) -> datetime | None:
    """Parse a TikTok export timestamp into a timezone-aware UTC datetime.

    TikTok writes naive timestamps like ``"2026-05-12 08:14:03"``. The
    export does not record the user's timezone, so we treat the value as
    UTC. Returns ``None`` if the string is unparsable.
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract_author(url: str) -> str | None:
    m = _AUTHOR_RE.search(url)
    return m.group(1) if m else None


def _coerce_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _load_export_payload(path: Path) -> dict[str, Any]:
    """Return the parsed JSON payload, transparently unpacking a zip.

    For a ``.zip`` (sniffed via :func:`zipfile.is_zipfile`, so a misnamed
    extension still works) we scan members and pick the first whose
    basename matches ``user_data.json`` case-insensitively. The file is
    read from the archive in memory — nothing is extracted to disk.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            member = _find_user_data_member(zf)
            if member is None:
                raise FileNotFoundError(
                    f"no user_data.json found in archive: {path.name}"
                )
            with zf.open(member) as fh:
                return json.loads(fh.read().decode("utf-8"))
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _find_user_data_member(zf: zipfile.ZipFile) -> str | None:
    """Locate ``user_data.json`` (case-insensitive) at any depth."""
    candidates = [
        info.filename for info in zf.infolist()
        if not info.is_dir()
        and Path(info.filename).name.lower() == "user_data.json"
    ]
    if not candidates:
        return None
    # Prefer the shallowest path so a top-level file wins over nested
    # duplicates inside vendor sub-archives.
    candidates.sort(key=lambda name: (name.count("/"), len(name)))
    return candidates[0]


def parse_export(path: str | Path) -> tuple[list[VideoMetadata], list[WatchEvent]]:
    """Parse a TikTok export into (videos, watch_events).

    ``path`` is either:
      * a JSON file (typically ``user_data.json``), or
      * a ``.zip`` archive that contains ``user_data.json`` at any depth.

    Other guarantees:
      - Videos are deduplicated by ``source_url`` (one row per unique URL).
      - Watch events preserve every playback occurrence in the export.
      - Order of videos follows first-seen order in the export; watch
        events follow source order.
      - The function never raises on missing optional fields; only a
        malformed JSON file or a file-system error will propagate.
    """
    p = Path(path)
    data = _load_export_payload(p)

    activity = (data or {}).get("Activity") or {}
    browsing = activity.get("Video Browsing History") or {}
    raw_entries = browsing.get("VideoList") or []

    videos: dict[str, VideoMetadata] = {}
    events: list[WatchEvent] = []
    skipped = 0

    for raw in raw_entries:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        url = raw.get("Link")
        date_raw = raw.get("Date")
        if not isinstance(url, str) or not url or not isinstance(date_raw, str) or not date_raw:
            skipped += 1
            continue
        watched_at = _parse_date(date_raw)
        if watched_at is None:
            skipped += 1
            continue

        video_id = stable_video_id(url)
        if video_id not in videos:
            videos[video_id] = VideoMetadata(
                id=video_id,
                source_url=url,
                title=None,
                author=_extract_author(url),
                duration_s=0.0,
                downloaded=False,
                local_path=None,
                tags=[],
            )

        events.append(
            WatchEvent(
                id=stable_watch_event_id(video_id, watched_at),
                video_id=video_id,
                watched_at=watched_at,
                duration_watched_s=_coerce_float(raw.get("DurationWatchedSeconds")),
                completion_pct=_coerce_float(raw.get("CompletionPct")),
                source="tiktok_export",
            )
        )

    if skipped:
        _log.debug("parse_export: skipped %d malformed entries", skipped)

    return list(videos.values()), events
