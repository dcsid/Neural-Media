"""SQLite-backed job queue that drives download → preprocess → infer.

Single-process, resumable, idempotent. The same SQLite file is read by
the api-orchestrator's ``SqliteStore`` (CONTRACTS.md §7) — the schema
below is the coordination surface between data-pipeline (writer) and
api (reader).

## Schema

Five tables. The first four mirror the records in ``shared/schemas.py``
and are read by ``SqliteStore``; the fifth (``pipeline_jobs``) is
private to the orchestrator and tracks job state for resume.

  videos              - VideoMetadata rows, one per unique source URL.
                        ``tags`` is stored as JSON in ``tags_json``;
                        ``inserted_at`` preserves first-seen order.
  watch_events        - WatchEvent rows. FK → videos(id).
  inference_runs      - InferenceRun rows. FK → videos(id).
  region_metrics      - RegionMetrics rows. ``timeseries`` is JSON.
                        PK = (inference_run_id, region_id).
  pipeline_jobs       - Per-video state machine. ``status`` walks:
                            queued → downloaded → preprocessed →
                            complete       (or failed at any step)

## Idempotence guarantees

  * ``ingest_export`` over the same export file twice is a no-op for
    rows that already exist (``INSERT OR IGNORE``).
  * Jobs with ``status='complete'`` are skipped by ``run_pending``.
  * Jobs with ``status='failed'`` are retried (attempts counter
    increments).
  * ``download_video`` and ``preprocess_video`` are themselves
    idempotent on disk (cache hits), so even a partial-state restart
    converges.

## Test seams

Every external side effect — yt-dlp, ffmpeg, ffprobe, run_inference —
is an injectable callable. Default wiring uses the real implementations;
tests pass stubs. There are NO live network or subprocess calls inside
the orchestrator itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from neural_media_inference import RunArtifacts, run_inference

from shared.schemas import InferenceRun, VideoMetadata, WatchEvent

from .downloader import (
    DownloadConfig,
    DownloadError,
    DownloadResult,
    FetchFn,
    SleepFn,
    _yt_dlp_fetch,
    download_video,
)
from .importer import parse_export
from .preprocess import (
    PreprocessConfig,
    PreprocessError,
    PreprocessResult,
    RunFn,
    _ffmpeg_run,
    preprocess_video,
)

_log = logging.getLogger(__name__)

# Job status state machine. Linear; failures are sticky until retry.
STATUS_QUEUED = "queued"
STATUS_DOWNLOADED = "downloaded"
STATUS_PREPROCESSED = "preprocessed"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

_DEFAULT_DURATION_S = 15.0
_DEFAULT_FMRI_SAMPLE_RATE_HZ = 1.5
_FALLBACK_TIMEPOINTS_MIN = 8  # ensures inference produces a usable run even
                              # if duration probe fails — protects mock backend.

# Injectable inference signature. Real callers default to run_inference.
InferenceFn = Callable[..., RunArtifacts]
ProbeFn = Callable[[Path], float]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    id           TEXT PRIMARY KEY,
    source_url   TEXT NOT NULL,
    title        TEXT,
    author       TEXT,
    duration_s   REAL NOT NULL DEFAULT 0.0,
    downloaded   INTEGER NOT NULL DEFAULT 0,
    local_path   TEXT,
    tags_json    TEXT NOT NULL DEFAULT '[]',
    inserted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_events (
    id                   TEXT PRIMARY KEY,
    video_id             TEXT NOT NULL REFERENCES videos(id),
    watched_at           TEXT NOT NULL,
    duration_watched_s   REAL,
    completion_pct       REAL,
    source               TEXT NOT NULL DEFAULT 'tiktok_export'
);
CREATE INDEX IF NOT EXISTS ix_watch_events_video_id ON watch_events(video_id);
CREATE INDEX IF NOT EXISTS ix_watch_events_watched_at ON watch_events(watched_at);

CREATE TABLE IF NOT EXISTS inference_runs (
    id                TEXT PRIMARY KEY,
    video_id          TEXT NOT NULL REFERENCES videos(id),
    model_id          TEXT NOT NULL,
    model_version     TEXT NOT NULL,
    seed              INTEGER NOT NULL,
    params_json       TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    activation_path   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS ix_inference_runs_video_id ON inference_runs(video_id);

CREATE TABLE IF NOT EXISTS region_metrics (
    inference_run_id  TEXT NOT NULL REFERENCES inference_runs(id),
    region_id         TEXT NOT NULL,
    video_id          TEXT NOT NULL,
    mean              REAL NOT NULL,
    peak              REAL NOT NULL,
    sustained         REAL NOT NULL,
    timeseries_json   TEXT NOT NULL,
    PRIMARY KEY (inference_run_id, region_id)
);
CREATE INDEX IF NOT EXISTS ix_region_metrics_video_id ON region_metrics(video_id);

CREATE TABLE IF NOT EXISTS pipeline_jobs (
    video_id            TEXT PRIMARY KEY REFERENCES videos(id),
    status              TEXT NOT NULL,
    last_error          TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    preprocessed_path   TEXT,
    inference_run_id    TEXT,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pipeline_jobs_status ON pipeline_jobs(status);
"""


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class IngestSummary:
    """Roll-up of one ingest invocation. Surfaced to the CLI."""

    parsed_videos: int = 0
    parsed_events: int = 0
    queued: int = 0
    completed: int = 0
    skipped_complete: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (video_id, msg)


@dataclass
class _JobOutcome:
    video_id: str
    status: str
    error: str | None = None
    artifacts: RunArtifacts | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_for(video_id: str) -> int:
    """Deterministic int seed in [0, 2**31). Same video → same seed → same run."""
    digest = hashlib.sha256(video_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _ffprobe_duration_s(path: Path) -> float:
    """System ffprobe → seconds. Mockable; raises on failure."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("ffprobe not on PATH")
    completed = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        check=False, capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ffprobe exited {completed.returncode}")
    raw = completed.stdout.decode("utf-8", errors="replace").strip()
    return float(raw) if raw else 0.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class OrchestratorConfig:
    db_path: Path
    videos_dir: Path
    processed_dir: Path
    activations_dir: Path
    fmri_sample_rate_hz: float = _DEFAULT_FMRI_SAMPLE_RATE_HZ
    default_duration_s: float = _DEFAULT_DURATION_S


class Orchestrator:
    """Drive a TikTok export end-to-end. Single-process, resumable."""

    def __init__(
        self,
        cfg: OrchestratorConfig,
        *,
        download_cfg: DownloadConfig | None = None,
        preprocess_cfg: PreprocessConfig | None = None,
        fetch: FetchFn = _yt_dlp_fetch,
        ffmpeg: RunFn = _ffmpeg_run,
        sleep: SleepFn | None = None,
        probe_duration: ProbeFn = _ffprobe_duration_s,
        inference_fn: InferenceFn = run_inference,
    ) -> None:
        self.cfg = cfg
        self.download_cfg = download_cfg or DownloadConfig(videos_dir=cfg.videos_dir)
        self.preprocess_cfg = preprocess_cfg or PreprocessConfig(processed_dir=cfg.processed_dir)
        self._fetch = fetch
        self._ffmpeg = ffmpeg
        self._sleep = sleep  # None → downloader uses time.sleep
        self._probe = probe_duration
        self._infer = inference_fn

        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.activations_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(cfg.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()

    # -- context manager support -----------------------------------------
    def __enter__(self) -> Orchestrator:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- public API -------------------------------------------------------

    def ingest_export(self, export_path: Path) -> IngestSummary:
        """Parse a TikTok export, upsert rows, then drive every pending job."""
        videos, events = parse_export(export_path)
        self._upsert_videos(videos)
        self._upsert_watch_events(events)
        queued = self._enqueue_pending(videos)

        summary = self.run_pending()
        summary.parsed_videos = len(videos)
        summary.parsed_events = len(events)
        summary.queued = queued
        return summary

    def run_pending(self) -> IngestSummary:
        """Drive all non-complete jobs to completion (or failure)."""
        summary = IngestSummary()
        for video_id in self._pending_job_ids():
            video = self._load_video(video_id)
            if video is None:
                continue
            outcome = self._drive_one(video)
            if outcome.status == STATUS_COMPLETE:
                summary.completed += 1
            elif outcome.status == STATUS_FAILED:
                summary.failed += 1
                summary.errors.append((video_id, outcome.error or "unknown"))
        return summary

    # -- DB I/O -----------------------------------------------------------

    def _upsert_videos(self, videos: list[VideoMetadata]) -> None:
        now = _utcnow_iso()
        with self._conn:
            for v in videos:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO videos
                        (id, source_url, title, author, duration_s,
                         downloaded, local_path, tags_json, inserted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (v.id, v.source_url, v.title, v.author, v.duration_s,
                     1 if v.downloaded else 0, v.local_path,
                     json.dumps(v.tags), now),
                )

    def _upsert_watch_events(self, events: list[WatchEvent]) -> None:
        with self._conn:
            for e in events:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO watch_events
                        (id, video_id, watched_at, duration_watched_s,
                         completion_pct, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (e.id, e.video_id, e.watched_at.isoformat(),
                     e.duration_watched_s, e.completion_pct, e.source),
                )

    def _enqueue_pending(self, videos: list[VideoMetadata]) -> int:
        """Insert a queued row for each video not already tracked."""
        now = _utcnow_iso()
        count = 0
        with self._conn:
            for v in videos:
                cur = self._conn.execute(
                    "SELECT 1 FROM pipeline_jobs WHERE video_id = ?", (v.id,),
                )
                if cur.fetchone():
                    continue
                self._conn.execute(
                    """
                    INSERT INTO pipeline_jobs
                        (video_id, status, attempts, updated_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (v.id, STATUS_QUEUED, now),
                )
                count += 1
        return count

    def _pending_job_ids(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT video_id FROM pipeline_jobs WHERE status != ? ORDER BY rowid",
            (STATUS_COMPLETE,),
        )
        return [row[0] for row in cur.fetchall()]

    def _load_video(self, video_id: str) -> VideoMetadata | None:
        cur = self._conn.execute(
            """
            SELECT id, source_url, title, author, duration_s,
                   downloaded, local_path, tags_json
              FROM videos WHERE id = ?
            """, (video_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return VideoMetadata(
            id=row[0], source_url=row[1], title=row[2], author=row[3],
            duration_s=row[4], downloaded=bool(row[5]), local_path=row[6],
            tags=json.loads(row[7]),
        )

    def _job_status(self, video_id: str) -> str | None:
        cur = self._conn.execute(
            "SELECT status FROM pipeline_jobs WHERE video_id = ?", (video_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def _job_field(self, video_id: str, field_name: str) -> Any:
        cur = self._conn.execute(
            f"SELECT {field_name} FROM pipeline_jobs WHERE video_id = ?",
            (video_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def _update_job(self, video_id: str, **fields: Any) -> None:
        fields["updated_at"] = _utcnow_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._conn:
            self._conn.execute(
                f"UPDATE pipeline_jobs SET {cols} WHERE video_id = ?",
                (*fields.values(), video_id),
            )

    def _bump_attempts(self, video_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE pipeline_jobs SET attempts = attempts + 1 WHERE video_id = ?",
                (video_id,),
            )

    # -- pipeline steps ---------------------------------------------------

    def _drive_one(self, video: VideoMetadata) -> _JobOutcome:
        """Walk one video from current status → complete (or failed)."""
        self._bump_attempts(video.id)
        status = self._job_status(video.id)

        try:
            if status in (STATUS_QUEUED, STATUS_FAILED, None):
                self._do_download(video)
                status = STATUS_DOWNLOADED
            if status == STATUS_DOWNLOADED:
                self._do_preprocess(video)
                status = STATUS_PREPROCESSED
            if status == STATUS_PREPROCESSED:
                artifacts = self._do_inference(video)
                return _JobOutcome(video_id=video.id, status=STATUS_COMPLETE,
                                   artifacts=artifacts)
        except (DownloadError, PreprocessError, RuntimeError, ValueError) as exc:
            self._update_job(video.id, status=STATUS_FAILED, last_error=str(exc))
            _log.warning("job failed for %s", video.id)  # no URL in log
            return _JobOutcome(video_id=video.id, status=STATUS_FAILED, error=str(exc))

        # status was already STATUS_COMPLETE
        return _JobOutcome(video_id=video.id, status=STATUS_COMPLETE)

    def _do_download(self, video: VideoMetadata) -> DownloadResult:
        res = download_video(
            video, self.download_cfg,
            fetch=self._fetch,
            **({"sleep": self._sleep} if self._sleep else {}),
        )
        if not res.ok or res.local_path is None:
            raise DownloadError(f"download did not produce a file for {video.id}")
        self._update_job(video.id, status=STATUS_DOWNLOADED)
        with self._conn:
            self._conn.execute(
                "UPDATE videos SET downloaded = 1, local_path = ? WHERE id = ?",
                (str(res.local_path), video.id),
            )
        return res

    def _do_preprocess(self, video: VideoMetadata) -> PreprocessResult:
        src = self.cfg.videos_dir / f"{video.id}.mp4"
        res = preprocess_video(src, video.id, self.preprocess_cfg, run=self._ffmpeg)
        self._update_job(
            video.id,
            status=STATUS_PREPROCESSED,
            preprocessed_path=str(res.local_path),
        )
        return res

    def _do_inference(self, video: VideoMetadata) -> RunArtifacts:
        processed_path = Path(self._job_field(video.id, "preprocessed_path"))

        duration_s = video.duration_s
        if duration_s <= 0.0:
            try:
                duration_s = self._probe(processed_path)
            except Exception as exc:  # noqa: BLE001
                _log.debug("ffprobe failed for %s: %s", video.id, exc)
                duration_s = 0.0
        if duration_s <= 0.0:
            duration_s = self.cfg.default_duration_s
        # Make sure even a tiny duration produces enough timepoints for the
        # mock backend's downsampler to be happy.
        min_duration = _FALLBACK_TIMEPOINTS_MIN / self.cfg.fmri_sample_rate_hz
        if duration_s < min_duration:
            duration_s = min_duration

        artifacts = self._infer(
            video_id=video.id,
            duration_s=duration_s,
            seed=_seed_for(video.id),
            sample_rate_hz=self.cfg.fmri_sample_rate_hz,
            activations_dir=self.cfg.activations_dir,
            extra_params=self.preprocess_cfg.extra_params(),
        )

        self._persist_run(artifacts)
        self._update_job(
            video.id,
            status=STATUS_COMPLETE,
            inference_run_id=artifacts.inference_run.id,
            last_error=None,
        )
        # Persist the downsampled ActivationOutput as JSON next to the npz
        # so SqliteStore can serve /videos/{id}/activation without
        # re-decompressing.
        payload_path = self.cfg.activations_dir / f"{artifacts.inference_run.id}.json"
        payload_path.write_text(artifacts.activation_payload.model_dump_json())
        # Also record duration probed back onto the video row for next time.
        with self._conn:
            self._conn.execute(
                "UPDATE videos SET duration_s = ? WHERE id = ? AND duration_s <= 0",
                (duration_s, video.id),
            )
        return artifacts

    def _persist_run(self, artifacts: RunArtifacts) -> None:
        run: InferenceRun = artifacts.inference_run
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO inference_runs
                    (id, video_id, model_id, model_version, seed,
                     params_json, created_at, activation_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run.id, run.video_id, run.model_id, run.model_version,
                 run.seed, json.dumps(run.params_json),
                 run.created_at.isoformat(), run.activation_path, run.status),
            )
            for m in artifacts.region_metrics:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO region_metrics
                        (inference_run_id, region_id, video_id,
                         mean, peak, sustained, timeseries_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (m.inference_run_id, m.region_id, m.video_id,
                     m.mean, m.peak, m.sustained, json.dumps(m.timeseries)),
                )


__all__ = [
    "IngestSummary",
    "Orchestrator",
    "OrchestratorConfig",
    "STATUS_COMPLETE",
    "STATUS_DOWNLOADED",
    "STATUS_FAILED",
    "STATUS_PREPROCESSED",
    "STATUS_QUEUED",
]
