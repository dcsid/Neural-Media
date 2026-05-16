"""Catalog stores backing the API.

Two implementations live behind one read-only interface (`Store`):

* `SampleStore` — reads pre-generated mock outputs from
  `data/sample/mock_inference/` so the vertical slice runs without GPU.
* `SqliteStore` — SQLite + Parquet/NPZ on disk; written by the data-pipeline
  worker, consumed here. Built once the pipeline lands.

The sample directory layout consumed by `SampleStore`:

    data/sample/mock_inference/
        videos.json           # list[VideoMetadata]
        watch_events.json     # list[WatchEvent]
        inference_runs.json   # list[InferenceRun]
        region_metrics.json   # list[RegionMetrics]
        activations/
            {inference_run_id}.json   # ActivationOutput (downsampled)

Any missing file degrades to an empty collection rather than raising, so the
API can start before the ml-inference worker's `build_sample_outputs.py`
script has run.
"""

from __future__ import annotations

import json
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


def _load_json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON list in {path}, got {type(data).__name__}")
    return data


class SampleStore:
    """File-backed read-only store for the vertical slice."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._videos: dict[str, VideoMetadata] = {}
        self._video_order: list[str] = []
        self._watch_events: list[WatchEvent] = []
        self._runs: list[InferenceRun] = []
        self._metrics_by_video: dict[str, list[RegionMetrics]] = {}
        self._activation_by_video: dict[str, ActivationOutput] = {}
        self._load()

    def _load(self) -> None:
        # Videos — preserve file order so /videos is stable.
        for raw in _load_json_list(self.root / "videos.json"):
            v = VideoMetadata.model_validate(raw)
            if v.id not in self._videos:
                self._videos[v.id] = v
                self._video_order.append(v.id)

        for raw in _load_json_list(self.root / "watch_events.json"):
            self._watch_events.append(WatchEvent.model_validate(raw))

        for raw in _load_json_list(self.root / "inference_runs.json"):
            self._runs.append(InferenceRun.model_validate(raw))

        for raw in _load_json_list(self.root / "region_metrics.json"):
            m = RegionMetrics.model_validate(raw)
            self._metrics_by_video.setdefault(m.video_id, []).append(m)

        # Activations: one file per inference_run; we key the lookup by video_id
        # via the inference_runs list. If multiple runs exist for one video, the
        # most recently created (by `created_at`) wins.
        runs_by_video: dict[str, InferenceRun] = {}
        for run in sorted(self._runs, key=lambda r: r.created_at):
            runs_by_video[run.video_id] = run

        activations_dir = self.root / "activations"
        if activations_dir.is_dir():
            for path in sorted(activations_dir.glob("*.json")):
                with path.open("r", encoding="utf-8") as fh:
                    payload = ActivationOutput.model_validate(json.load(fh))
                self._activation_by_video[payload.video_id] = payload

    # --- read API ---------------------------------------------------------

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
        return list(self._runs)
