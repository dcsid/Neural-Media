"""Catalog stores backing the API.

Two implementations live behind one read-only interface (`Store`):

* `SampleStore` — reads pre-generated mock outputs from
  `data/sample/mock_inference/` and a TikTok export at
  `data/sample/tiktok_export/user_data.json`. Used for the vertical slice
  so the API runs without GPU and without the data-pipeline writing to
  SQLite.
* `SqliteStore` — SQLite + on-disk activations; consumed here, written by
  the data-pipeline worker. See `sqlite_store.py`.

The sample directory layout consumed by `SampleStore` is whatever
`services/inference/scripts/build_sample_outputs.py` produces:

    data/sample/mock_inference/
        index.json                            # manifest of videos + runs
        {video_id}.run.json                   # InferenceRun
        {video_id}.metrics.json               # list[RegionMetrics]
        {video_id}.activation.json            # ActivationOutput (wire fmt)

`VideoMetadata` is reconstructed from `index.json` + URL parsing.
`WatchEvent` rows come from running `neural_media_pipeline.parse_export`
against the committed TikTok export — same parser the real pipeline uses,
so any drift in the importer is visible end-to-end in the sample slice.

Any missing file degrades to an empty collection rather than raising, so
the API stays up even before the sample fixtures or the export exist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from .schemas_re_export import (
    ActivationOutput,
    InferenceRun,
    RegionMetrics,
    VideoMetadata,
    WatchEvent,
)


class Store(Protocol):
    """Read interface used by route handlers. Both store impls satisfy this."""

    def list_videos(self) -> list[VideoMetadata]: ...
    def get_video(self, video_id: str) -> VideoMetadata | None: ...
    def get_metrics(self, video_id: str) -> list[RegionMetrics]: ...
    def get_activation(self, video_id: str) -> ActivationOutput | None: ...
    def list_watch_events(self) -> list[WatchEvent]: ...
    def list_runs(self) -> list[InferenceRun]: ...
    def version(self) -> str:
        """Opaque marker that changes whenever the store's contents change.

        Used by `compute_aggregate` to memoize the AggregateReport across
        requests. Implementations should make this cheap — the aggregator
        calls it on every hit.
        """
        ...


def _load_json(path: Path) -> dict | list | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


_AUTHOR_RE = re.compile(r"/@([^/]+)/")


def _author_from_url(url: str) -> str | None:
    m = _AUTHOR_RE.search(url)
    return m.group(1) if m else None


class SampleStore:
    """File-backed read-only store for the vertical slice.

    `mock_inference_root` is the directory the ml-inference worker writes
    inference outputs into. `tiktok_export_path` is the JSON the
    data-pipeline importer reads to produce watch events; pass `None` to
    skip watch-event hydration (the API still serves videos / metrics /
    activations from the mock outputs).
    """

    def __init__(
        self,
        mock_inference_root: Path | str,
        tiktok_export_path: Path | str | None = None,
    ) -> None:
        self.mock_inference_root = Path(mock_inference_root)
        self.tiktok_export_path = (
            Path(tiktok_export_path) if tiktok_export_path is not None else None
        )

        self._videos: dict[str, VideoMetadata] = {}
        self._video_order: list[str] = []
        self._watch_events: list[WatchEvent] = []
        self._runs_by_video: dict[str, InferenceRun] = {}
        self._metrics_by_video: dict[str, list[RegionMetrics]] = {}
        self._activation_by_video: dict[str, ActivationOutput] = {}
        # Snapshot of inputs at __init__ time, used by `version()`. Stored
        # as a tuple of (path, mtime_ns) so a regenerated fixture is
        # detectable without rescanning the directory.
        self._version_signature: str = ""
        self._load()

    # --- loaders ---------------------------------------------------------

    def _load(self) -> None:
        videos_from_export = self._load_export()
        videos_from_index = self._load_videos_from_index()

        # Export-derived videos win when both are present: the export has
        # the canonical metadata (title, tags). Index entries fill in any
        # videos that aren't in the export (shouldn't normally happen).
        for v in videos_from_export + videos_from_index:
            if v.id in self._videos:
                continue
            self._videos[v.id] = v
            self._video_order.append(v.id)

        self._load_runs_and_metrics()
        self._load_activations()
        self._version_signature = self._compute_signature()

    def _load_export(self) -> list[VideoMetadata]:
        """Run the canonical TikTok-export parser to get videos + events."""
        if self.tiktok_export_path is None or not self.tiktok_export_path.is_file():
            return []
        # Lazy import: the pipeline package is only used by SampleStore;
        # SqliteStore has no dep on it.
        from neural_media_pipeline import parse_export

        videos, events = parse_export(self.tiktok_export_path)
        self._watch_events.extend(events)
        return list(videos)

    def _load_videos_from_index(self) -> list[VideoMetadata]:
        raw = _load_json(self.mock_inference_root / "index.json")
        if not isinstance(raw, dict):
            return []
        entries = raw.get("videos") or []
        out: list[VideoMetadata] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vid = entry.get("video_id")
            url = entry.get("source_url")
            if not vid or not url:
                continue
            out.append(VideoMetadata(
                id=vid,
                source_url=url,
                title=None,
                author=_author_from_url(url),
                duration_s=float(entry.get("duration_s", 0.0) or 0.0),
                downloaded=False,
                local_path=None,
                tags=[],
            ))
        return out

    def _load_runs_and_metrics(self) -> None:
        if not self.mock_inference_root.is_dir():
            return
        for run_path in sorted(self.mock_inference_root.glob("*.run.json")):
            raw = _load_json(run_path)
            if not isinstance(raw, dict):
                continue
            run = InferenceRun.model_validate(raw)
            # Keep only the most recent run per video.
            existing = self._runs_by_video.get(run.video_id)
            if existing is None or run.created_at > existing.created_at:
                self._runs_by_video[run.video_id] = run

        for metrics_path in sorted(self.mock_inference_root.glob("*.metrics.json")):
            raw = _load_json(metrics_path)
            if not isinstance(raw, list):
                continue
            for row in raw:
                m = RegionMetrics.model_validate(row)
                self._metrics_by_video.setdefault(m.video_id, []).append(m)

    def _load_activations(self) -> None:
        if not self.mock_inference_root.is_dir():
            return
        for activation_path in sorted(self.mock_inference_root.glob("*.activation.json")):
            raw = _load_json(activation_path)
            if not isinstance(raw, dict):
                continue
            payload = ActivationOutput.model_validate(raw)
            self._activation_by_video[payload.video_id] = payload

    def _compute_signature(self) -> str:
        """Tag built from input mtimes so `version()` is cheap and stable.

        Reads stat() on the few sample files — not every glob match — so
        adding a new run regenerates `index.json` and bumps the version.
        """
        parts: list[str] = []
        for path in (
            self.mock_inference_root / "index.json",
            self.tiktok_export_path,
        ):
            if path is None:
                continue
            try:
                parts.append(f"{path.name}:{path.stat().st_mtime_ns}")
            except (FileNotFoundError, OSError):
                parts.append(f"{path.name}:missing")
        parts.append(f"runs:{len(self._runs_by_video)}")
        parts.append(f"events:{len(self._watch_events)}")
        return "|".join(parts)

    # --- Store protocol --------------------------------------------------

    def list_videos(self) -> list[VideoMetadata]:
        return [self._videos[vid] for vid in self._video_order]

    def get_video(self, video_id: str) -> VideoMetadata | None:
        return self._videos.get(video_id)

    def get_metrics(self, video_id: str) -> list[RegionMetrics]:
        return list(self._metrics_by_video.get(video_id, ()))

    def get_activation(self, video_id: str) -> ActivationOutput | None:
        return self._activation_by_video.get(video_id)

    def list_watch_events(self) -> list[WatchEvent]:
        return list(self._watch_events)

    def list_runs(self) -> list[InferenceRun]:
        # Stable order: by created_at so callers can rely on chronology.
        return sorted(self._runs_by_video.values(), key=lambda r: r.created_at)

    def version(self) -> str:
        return self._version_signature
