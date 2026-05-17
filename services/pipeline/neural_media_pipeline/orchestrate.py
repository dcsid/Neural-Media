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
                        ``first_seen_idx`` preserves first-seen order
                        and is read by api-orchestrator's SqliteStore
                        for the canonical `/api/v1/videos` ordering.
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
import inspect
import json
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from neural_media_inference import InferenceBackend, RunArtifacts, run_inference

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

# Progress callback surface — consumed by the api-orchestrator's
# /api/v1/import endpoint to drive the dashboard's progress UI.
Phase = Literal["parsing", "downloading", "preprocessing", "inferring"]
ProgressCallback = Callable[["ProgressEvent"], None]


@dataclass(frozen=True)
class ProgressEvent:
    """One step in the ingest lifecycle.

    Emitted at phase boundaries (one per video, per step) and once after
    each video's inference completes with an incremented
    ``videos_processed`` counter. ``message`` is the video id when the
    event is bound to a specific video — never the source URL.
    """

    phase: Phase
    videos_total: int = 0
    videos_processed: int = 0
    message: str | None = None


def _synth_duration_s(video_id: str) -> float:
    """Deterministic synthetic duration for mock mode.

    Mirrors ``services/inference/scripts/build_sample_outputs.py``'s
    ``_duration_for`` so the demo flow produces the same timeseries
    lengths as the committed sample fixtures.
    """
    h = int.from_bytes(hashlib.sha256(video_id.encode()).digest()[:4], "big")
    return 15.0 + (h % 46)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- The four tables below MUST match
-- services/api/neural_media_api/sqlite_store.py::SCHEMA_STATEMENTS byte-
-- for-byte on column names / types / defaults. api is the consumer; any
-- drift here breaks /api/v1/* reads. ``import_jobs`` is owned by the api
-- worker and intentionally NOT created here.
CREATE TABLE IF NOT EXISTS videos (
    id              TEXT PRIMARY KEY,
    source_url      TEXT NOT NULL,
    title           TEXT,
    author          TEXT,
    duration_s      REAL NOT NULL DEFAULT 0.0,
    downloaded      INTEGER NOT NULL DEFAULT 0,
    local_path      TEXT,
    tags_json       TEXT NOT NULL DEFAULT '[]',
    first_seen_idx  INTEGER
);

CREATE TABLE IF NOT EXISTS watch_events (
    id                  TEXT PRIMARY KEY,
    video_id            TEXT NOT NULL,
    watched_at          TEXT NOT NULL,
    duration_watched_s  REAL,
    completion_pct      REAL,
    source              TEXT NOT NULL DEFAULT 'tiktok_export'
);
CREATE INDEX IF NOT EXISTS idx_watch_events_watched_at ON watch_events(watched_at);
CREATE INDEX IF NOT EXISTS idx_watch_events_video_id ON watch_events(video_id);

CREATE TABLE IF NOT EXISTS inference_runs (
    id               TEXT PRIMARY KEY,
    video_id         TEXT NOT NULL,
    model_id         TEXT NOT NULL,
    model_version    TEXT NOT NULL,
    seed             INTEGER NOT NULL,
    params_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    activation_path  TEXT NOT NULL,
    status           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inference_runs_video_id ON inference_runs(video_id);
CREATE INDEX IF NOT EXISTS idx_inference_runs_created_at ON inference_runs(created_at);

CREATE TABLE IF NOT EXISTS region_metrics (
    inference_run_id  TEXT NOT NULL,
    region_id         TEXT NOT NULL,
    video_id          TEXT NOT NULL,
    mean              REAL NOT NULL,
    peak              REAL NOT NULL,
    sustained         REAL NOT NULL,
    timeseries_json   TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (inference_run_id, region_id)
);
CREATE INDEX IF NOT EXISTS idx_region_metrics_video_id ON region_metrics(video_id);

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
    """Runtime config for the orchestrator.

    Provide either ``data_root`` (and the four path fields default to
    ``<root>/sqlite/neural_media.db``, ``<root>/videos`` etc.) or all
    four paths explicitly. Mixed usage is also fine: any explicitly-set
    path wins over the ``data_root``-derived default.

    ``skip_download`` and ``skip_preprocess`` are the demo-mode toggles
    — see the module docstring. With both true the orchestrator never
    touches yt-dlp or ffmpeg and the synthesized per-video duration
    matches the committed sample fixtures.

    ``backend`` is forwarded to ``run_inference`` so callers can plug in
    MockBackend (default-by-omission) or a real TRIBE backend without
    re-wiring the test seam.
    """

    db_path: Path | None = None
    videos_dir: Path | None = None
    processed_dir: Path | None = None
    activations_dir: Path | None = None
    data_root: Path | None = None
    skip_download: bool = False
    skip_preprocess: bool = False
    backend: InferenceBackend | None = None
    # When True, ask the inference runner to skip zlib compression on
    # activation persistence. ``None`` (default) means "don't pass the
    # arg" — the runner currently does not expose it, so we sniff for
    # support and forward iff the parameter exists. Mock mode (both
    # skip flags) opts in automatically because that's the demo path
    # where the 23-seconds-of-zlib was profiled away.
    compress_activations: bool | None = None
    fmri_sample_rate_hz: float = _DEFAULT_FMRI_SAMPLE_RATE_HZ
    default_duration_s: float = _DEFAULT_DURATION_S

    def __post_init__(self) -> None:
        if self.data_root is not None:
            root = Path(self.data_root)
            self.db_path = self.db_path or root / "sqlite" / "neural_media.db"
            self.videos_dir = self.videos_dir or root / "videos"
            self.processed_dir = self.processed_dir or root / "videos_processed"
            self.activations_dir = self.activations_dir or root / "activations"
        missing = [
            name for name in ("db_path", "videos_dir", "processed_dir", "activations_dir")
            if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                f"OrchestratorConfig: must set {', '.join(missing)} "
                "(or pass data_root)"
            )


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
        # Sniff once whether the configured inference fn supports the
        # `compress` kwarg. Avoids try/except on every call and keeps
        # the test seam (fakes use **kw and would silently swallow it).
        try:
            self._infer_accepts_compress = (
                "compress" in inspect.signature(inference_fn).parameters
            )
        except (TypeError, ValueError):
            self._infer_accepts_compress = False

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

    def run(
        self,
        export_path: Path | str,
        *,
        progress: ProgressCallback | None = None,
    ) -> IngestSummary:
        """Parse export → upsert → drive every pending job. Emits progress.

        This is the canonical programmatic entry point — the in-process
        equivalent of ``python -m neural_media_pipeline ...``. The api
        worker calls it from a background thread off ``/api/v1/import``.

        ``progress`` is invoked at every phase boundary and once more
        after each video completes inference (with an incremented
        ``videos_processed``). The callback is synchronous — the api
        worker is responsible for queueing/serialising if it cares.
        """
        cb: ProgressCallback = progress or (lambda _e: None)

        cb(ProgressEvent(phase="parsing", message="parsing export"))
        videos, events = parse_export(export_path)
        self._upsert_videos(videos)
        self._upsert_watch_events(events)
        queued = self._enqueue_pending(videos)

        summary = self._drive_all(cb)
        summary.parsed_videos = len(videos)
        summary.parsed_events = len(events)
        summary.queued = queued
        return summary

    # Back-compat name: thin wrapper around `run`.
    def ingest_export(self, export_path: Path) -> IngestSummary:
        return self.run(export_path)

    def run_pending(
        self, *, progress: ProgressCallback | None = None,
    ) -> IngestSummary:
        """Drive every non-complete job without re-parsing an export."""
        cb: ProgressCallback = progress or (lambda _e: None)
        return self._drive_all(cb)

    # --- inner loop ------------------------------------------------------

    def _drive_all(self, cb: ProgressCallback) -> IngestSummary:
        summary = IngestSummary()
        pending = self._pending_job_ids()
        total = len(pending)
        # Emit the "switching out of parsing" event with totals known.
        if total > 0:
            first_phase: Phase = (
                "inferring" if self.cfg.skip_download and self.cfg.skip_preprocess
                else "preprocessing" if self.cfg.skip_download
                else "downloading"
            )
            cb(ProgressEvent(phase=first_phase, videos_total=total, videos_processed=0))

        processed = 0
        for video_id in pending:
            video = self._load_video(video_id)
            if video is None:
                continue
            outcome = self._drive_one(video, cb=cb, total=total, processed=processed)
            if outcome.status == STATUS_COMPLETE:
                summary.completed += 1
            elif outcome.status == STATUS_FAILED:
                summary.failed += 1
                summary.errors.append((video_id, outcome.error or "unknown"))
            processed += 1
            # End-of-video event: advances `videos_processed`. The api
            # worker uses this to update its job-status row.
            cb(ProgressEvent(
                phase="inferring",
                videos_total=total,
                videos_processed=processed,
                message=video_id,
            ))
        return summary

    # -- DB I/O -----------------------------------------------------------

    def _upsert_videos(self, videos: list[VideoMetadata]) -> None:
        if not videos:
            return
        # Continue numbering from where the table left off so subsequent
        # imports keep stable first-seen order for /api/v1/videos. Gaps
        # (from INSERT OR IGNORE skips) are fine — the column only needs
        # to be monotonic, not dense.
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(first_seen_idx), -1) FROM videos"
        )
        next_idx = int(cur.fetchone()[0]) + 1
        with self._conn:
            for i, v in enumerate(videos):
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO videos
                        (id, source_url, title, author, duration_s,
                         downloaded, local_path, tags_json, first_seen_idx)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (v.id, v.source_url, v.title, v.author, v.duration_s,
                     1 if v.downloaded else 0, v.local_path,
                     json.dumps(v.tags), next_idx + i),
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

    def _drive_one(
        self,
        video: VideoMetadata,
        *,
        cb: ProgressCallback,
        total: int,
        processed: int,
    ) -> _JobOutcome:
        """Walk one video from current status → complete (or failed)."""
        self._bump_attempts(video.id)
        status = self._job_status(video.id)

        try:
            if status in (STATUS_QUEUED, STATUS_FAILED, None):
                if self.cfg.skip_download:
                    self._mark_download_skipped(video)
                else:
                    cb(ProgressEvent(
                        phase="downloading", videos_total=total,
                        videos_processed=processed, message=video.id,
                    ))
                    self._do_download(video)
                status = STATUS_DOWNLOADED
            if status == STATUS_DOWNLOADED:
                if self.cfg.skip_preprocess:
                    self._mark_preprocess_skipped(video)
                else:
                    cb(ProgressEvent(
                        phase="preprocessing", videos_total=total,
                        videos_processed=processed, message=video.id,
                    ))
                    self._do_preprocess(video)
                status = STATUS_PREPROCESSED
            if status == STATUS_PREPROCESSED:
                cb(ProgressEvent(
                    phase="inferring", videos_total=total,
                    videos_processed=processed, message=video.id,
                ))
                artifacts = self._do_inference(video)
                return _JobOutcome(video_id=video.id, status=STATUS_COMPLETE,
                                   artifacts=artifacts)
        except (DownloadError, PreprocessError, RuntimeError, ValueError) as exc:
            self._update_job(video.id, status=STATUS_FAILED, last_error=str(exc))
            _log.warning("job failed for %s", video.id)  # no URL in log
            return _JobOutcome(video_id=video.id, status=STATUS_FAILED, error=str(exc))

        # status was already STATUS_COMPLETE
        return _JobOutcome(video_id=video.id, status=STATUS_COMPLETE)

    def _mark_download_skipped(self, video: VideoMetadata) -> None:
        """Mock-mode shortcut: advance status without touching the network.

        Leaves ``videos.downloaded=0`` and ``videos.local_path=NULL`` —
        the demo path explicitly does NOT pretend a file is on disk.
        """
        self._update_job(video.id, status=STATUS_DOWNLOADED)

    def _mark_preprocess_skipped(self, video: VideoMetadata) -> None:
        """Mock-mode shortcut: advance status without invoking ffmpeg."""
        self._update_job(
            video.id, status=STATUS_PREPROCESSED, preprocessed_path=None,
        )

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
        duration_s = self._resolve_duration_s(video)

        infer_kwargs: dict[str, Any] = dict(
            video_id=video.id,
            duration_s=duration_s,
            seed=_seed_for(video.id),
            sample_rate_hz=self.cfg.fmri_sample_rate_hz,
            activations_dir=self.cfg.activations_dir,
            extra_params=self.preprocess_cfg.extra_params(),
        )
        if self.cfg.backend is not None:
            infer_kwargs["backend"] = self.cfg.backend
        compress = self._resolve_compress_flag()
        if compress is not None:
            infer_kwargs["compress"] = compress
        artifacts = self._infer(**infer_kwargs)

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

    def _resolve_compress_flag(self) -> bool | None:
        """Pick the value to pass for ``run_inference(compress=...)``.

        We only forward when the runner actually has that parameter — if
        it doesn't, kwargs would be rejected. Today's runner doesn't
        expose it; the moment ml-inference adds the slot, mock-mode runs
        automatically opt out of zlib (saves ~115 ms/video on the
        documented profile).

        Resolution order:
          1. Explicit ``OrchestratorConfig.compress_activations``.
          2. False when both skip flags are set (mock/demo mode).
          3. None — i.e. don't pass the kwarg at all.
        """
        if not self._infer_accepts_compress:
            return None
        if self.cfg.compress_activations is not None:
            return self.cfg.compress_activations
        if self.cfg.skip_download and self.cfg.skip_preprocess:
            return False
        return None

    def _resolve_duration_s(self, video: VideoMetadata) -> float:
        """Pick the most reliable duration for an inference call.

        Priority order:
          1. ``videos.duration_s`` if non-zero (we cache probed values).
          2. ffprobe over the preprocessed file, when we have one.
          3. The mock-mode synthetic duration (deterministic in
             video_id, matches the sample-fixture builder).
          4. ``default_duration_s`` as the final fallback.

        A floor of ``_FALLBACK_TIMEPOINTS_MIN / sample_rate_hz`` ensures
        even a tiny duration produces enough timepoints for the mock
        backend's downsampler.
        """
        duration_s = video.duration_s
        if duration_s <= 0.0 and not self.cfg.skip_preprocess:
            processed_path = self._job_field(video.id, "preprocessed_path")
            if processed_path:
                try:
                    duration_s = self._probe(Path(processed_path))
                except Exception as exc:  # noqa: BLE001
                    _log.debug("ffprobe failed for %s: %s", video.id, exc)
                    duration_s = 0.0
        if duration_s <= 0.0 and self.cfg.skip_preprocess:
            duration_s = _synth_duration_s(video.id)
        if duration_s <= 0.0:
            duration_s = self.cfg.default_duration_s

        min_duration = _FALLBACK_TIMEPOINTS_MIN / self.cfg.fmri_sample_rate_hz
        return max(duration_s, min_duration)

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
    "Phase",
    "ProgressCallback",
    "ProgressEvent",
    "STATUS_COMPLETE",
    "STATUS_DOWNLOADED",
    "STATUS_FAILED",
    "STATUS_PREPROCESSED",
    "STATUS_QUEUED",
]
