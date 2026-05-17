"""Import-job orchestration.

A POST to ``/api/v1/import`` lands a TikTok export on disk, registers an
ImportJob row, and hands the file off to
``neural_media_pipeline.Orchestrator`` on a background daemon thread.
Polling ``GET /api/v1/import/{id}`` returns the current ImportJob row —
the same row is persisted in SQLite so a crash mid-run leaves a
recoverable trail.

Wire shape: see ``shared/CONTRACTS.md §8`` and ``shared.schemas.ImportJob``.
Top-level status is one of ``queued | running | complete | partial |
failed``; progress detail (phase, counters) lives on the nested
``progress`` block.

## Concurrency model

Single user, local-first — at most one orchestrator runs at a time. A
``threading.Lock`` + a module-level "currently running job id" together
serve as the gate. A second POST while a job is running raises
``JobAlreadyRunning`` (the route handler turns this into 409 carrying
the in-flight job as the literal ImportJob shape).

## Progress reporting

The Orchestrator emits ``ProgressEvent``s through an ``on_progress=``
callback when it accepts one (data-pipeline shipped this in round 3).
We probe for it via ``inspect.signature``; until it's wired in older
checkouts, a sibling poller thread reads the orchestrator's own
``pipeline_jobs`` table to fill counters. Either signal updates
``progress_current`` / ``progress_total`` / ``progress_phase``; neither
changes the top-level status (which only flips at submission and at
terminal).

## Modes

``mock`` (default) uses the deterministic MockBackend that ships with
neural-media-inference's base install. ``real`` requires the ``[real]``
extra (torch + tribev2 + weights) and is rejected with 400 if the
extra isn't installed.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .schemas_re_export import (
    ImportJob,
    ImportJobProgress,
    ImportJobStatus,
    ImportMode,
)

_log = logging.getLogger(__name__)

# Tighter bound than the pipeline's own poll loop — UI polling is once-
# per-second-ish, so 0.5s gives us a low-jitter signal without spinning.
_POLL_INTERVAL_S = 0.5

# Statuses considered terminal — the gate releases on entry to one of
# these, and orphan recovery flips anything non-terminal to "failed".
_TERMINAL_STATUSES: frozenset[ImportJobStatus] = frozenset(
    {"complete", "partial", "failed"}
)

# Map from pipeline_jobs.status (data-pipeline's per-video state machine)
# to a phase label suitable for `progress.phase`. We pick the
# "furthest along" not-yet-complete row each tick so the UI shows
# forward motion, not regress.
_PIPELINE_TO_PHASE: dict[str, str] = {
    "queued":       "downloading",
    "downloaded":   "preprocessing",
    "preprocessed": "inferring",
    "complete":     "inferring",   # still in flight until all rows clear
    "failed":       "inferring",   # one failure doesn't flip the job's phase
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class ImportJobsStore:
    """Thin CRUD over the ``import_jobs`` table.

    Per-call connections, same pattern as ``SqliteStore``, so this is safe
    to share between the request thread and the runner / poller threads.
    The orchestrator opens its own connection on the same file with
    ``journal_mode=WAL``, which lets us issue concurrent reads here.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Be a polite reader — the orchestrator may be writing.
        conn.execute("PRAGMA busy_timeout = 2000")
        return conn

    def create(self, job: ImportJob) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO import_jobs (
                    id, status, mode,
                    created_at, updated_at, completed_at,
                    progress_current, progress_total, progress_phase,
                    error, source_filename
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id, job.status, job.mode,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.completed_at.isoformat() if job.completed_at else None,
                    job.progress.current,
                    job.progress.total,
                    job.progress.phase,
                    job.error,
                    job.source_filename,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, job_id: str) -> ImportJob | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM import_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
        return _decode_row(row) if row else None

    def update(self, job_id: str, **fields: Any) -> None:
        """Patch one or more columns.

        Accepts top-level columns (``status``, ``completed_at``, ``error``,
        ``source_filename``) and nested progress fields (``progress_current``,
        ``progress_total``, ``progress_phase``). ``updated_at`` is always
        stamped to now — callers should not pass it.
        """
        if not fields:
            return
        fields["updated_at"] = _utcnow().isoformat()
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [
            v.isoformat() if isinstance(v, datetime) else v
            for v in fields.values()
        ]
        conn = self._connect()
        try:
            conn.execute(
                f"UPDATE import_jobs SET {cols} WHERE id = ?",
                (*values, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    def latest_running(self) -> ImportJob | None:
        """Most-recently-started non-terminal job, if any."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM import_jobs
                 WHERE status NOT IN ('complete', 'partial', 'failed')
                 ORDER BY created_at DESC
                 LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
        return _decode_row(row) if row else None

    def mark_orphans_failed(self) -> int:
        """At startup, any non-terminal row left over from a crashed
        prior process is no longer actually running. Flip them to
        ``failed`` so the gate doesn't permanently refuse new imports."""
        conn = self._connect()
        try:
            now = _utcnow().isoformat()
            cur = conn.execute(
                """
                UPDATE import_jobs
                   SET status = 'failed',
                       error = COALESCE(error, 'orphaned by api restart'),
                       updated_at = ?,
                       completed_at = ?
                 WHERE status NOT IN ('complete', 'partial', 'failed')
                """,
                (now, now),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def _decode_row(row: sqlite3.Row) -> ImportJob:
    return ImportJob(
        id=row["id"],
        status=row["status"],
        mode=row["mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        progress=ImportJobProgress(
            current=row["progress_current"],
            total=row["progress_total"],
            phase=row["progress_phase"],
        ),
        error=row["error"],
        source_filename=row["source_filename"],
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# OrchestratorFactory takes (db_path, activations_dir, videos_dir,
# processed_dir, mode, on_progress) and returns an object that exposes
# ``ingest_export(path) -> IngestSummary`` and ``close()``. Real wiring
# constructs neural_media_pipeline.Orchestrator; tests inject a stub.
OrchestratorFactory = Callable[..., Any]

ProgressCallback = Callable[[dict], None]


class JobAlreadyRunning(RuntimeError):
    """POST /import while another job is in flight."""

    def __init__(self, running_job: ImportJob) -> None:
        super().__init__(f"import job {running_job.id} is still running")
        self.running_job = running_job


class ImportRunner:
    """Owns the singleton-job gate and the background worker thread.

    Construction takes a factory so tests can swap in a stub orchestrator
    that runs synchronously without touching yt-dlp / ffmpeg / TRIBE.
    """

    def __init__(
        self,
        db_path: Path | str,
        activations_dir: Path | str,
        videos_dir: Path | str,
        processed_dir: Path | str,
        orchestrator_factory: OrchestratorFactory,
    ) -> None:
        self.db_path = Path(db_path)
        self.activations_dir = Path(activations_dir)
        self.videos_dir = Path(videos_dir)
        self.processed_dir = Path(processed_dir)
        self._factory = orchestrator_factory
        self._jobs = ImportJobsStore(db_path)
        self._lock = threading.Lock()
        self._running_job_id: str | None = None
        # Recover from a crashed prior process: nothing is actually running
        # at construction time, no matter what stale rows say.
        self._jobs.mark_orphans_failed()

    # --- public ----------------------------------------------------------

    @property
    def jobs(self) -> ImportJobsStore:
        return self._jobs

    def submit(
        self,
        export_path: Path,
        mode: ImportMode,
        *,
        source_filename: str | None = None,
    ) -> ImportJob:
        """Register a queued job and start its worker thread.

        Raises ``JobAlreadyRunning`` if another job is in flight.
        """
        with self._lock:
            if self._running_job_id is not None:
                running = self._jobs.get(self._running_job_id)
                if running and running.status not in _TERMINAL_STATUSES:
                    raise JobAlreadyRunning(running)
                # Stale slot — clear it and proceed.
                self._running_job_id = None

            now = _utcnow()
            job = ImportJob(
                id=str(uuid.uuid4()),
                status="queued",
                mode=mode,
                created_at=now,
                updated_at=now,
                source_filename=source_filename,
            )
            self._jobs.create(job)
            self._running_job_id = job.id

        thread = threading.Thread(
            target=self._run,
            args=(job.id, export_path, mode),
            daemon=True,
            name=f"import-{job.id[:8]}",
        )
        thread.start()
        return job

    # --- internals -------------------------------------------------------

    def _run(self, job_id: str, export_path: Path, mode: ImportMode) -> None:
        """Background-thread entry point. Owns one orchestrator lifetime."""
        poller_stop = threading.Event()
        poller: threading.Thread | None = None
        orch = None
        try:
            # Flip the top-level status once; phase tells the story from here.
            self._jobs.update(
                job_id,
                status="running",
                progress_phase="parsing",
            )

            orch = self._factory(
                db_path=self.db_path,
                activations_dir=self.activations_dir,
                videos_dir=self.videos_dir,
                processed_dir=self.processed_dir,
                mode=mode,
                on_progress=_progress_callback(self._jobs, job_id),
            )

            # The on_progress callback only fires if data-pipeline wired
            # support for it. Polling pipeline_jobs covers the gap when it
            # didn't (older checkouts).
            poller = threading.Thread(
                target=_poll_pipeline_jobs,
                args=(self.db_path, self._jobs, job_id, poller_stop),
                daemon=True,
                name=f"import-poll-{job_id[:8]}",
            )
            poller.start()

            summary = orch.ingest_export(export_path)

            completed = (
                getattr(summary, "completed", 0)
                + getattr(summary, "skipped_complete", 0)
            )
            failed = getattr(summary, "failed", 0)
            parsed = getattr(summary, "parsed_videos", completed + failed)

            terminal: ImportJobStatus
            if failed == 0:
                terminal = "complete"
            elif completed > 0:
                terminal = "partial"
            else:
                terminal = "failed"

            self._jobs.update(
                job_id,
                status=terminal,
                progress_current=completed,
                progress_total=parsed,
                progress_phase=None,
                completed_at=_utcnow(),
            )
        except Exception as exc:  # noqa: BLE001 — surface every failure
            _log.exception("import job %s failed", job_id)
            self._jobs.update(
                job_id,
                status="failed",
                error=str(exc),
                completed_at=_utcnow(),
            )
        finally:
            poller_stop.set()
            if poller is not None:
                poller.join(timeout=2.0)
            if orch is not None and hasattr(orch, "close"):
                try:
                    orch.close()
                except Exception:  # noqa: BLE001
                    _log.exception("closing orchestrator for %s raised", job_id)
            with self._lock:
                if self._running_job_id == job_id:
                    self._running_job_id = None


# ---------------------------------------------------------------------------
# Progress reporting helpers
# ---------------------------------------------------------------------------

def _progress_callback(jobs: ImportJobsStore, job_id: str) -> ProgressCallback:
    """Build the on_progress callback we hand to the Orchestrator.

    Accepts a free-form dict so we don't fail on schema drift. Recognised
    keys: ``phase`` / ``stage`` (either spelling), ``videos_total``,
    ``videos_processed``, plus an optional ``current``/``total``/``phase``
    cluster for callers that already speak the canonical shape.
    """
    def _cb(event: dict) -> None:
        fields: dict[str, Any] = {}

        phase = event.get("phase") or event.get("stage")
        if phase is not None:
            fields["progress_phase"] = str(phase)

        # Accept either flat (videos_total / videos_processed) or nested-
        # style (current / total) field names.
        if "videos_total" in event:
            fields["progress_total"] = event["videos_total"]
        elif "total" in event:
            fields["progress_total"] = event["total"]

        if "videos_processed" in event:
            fields["progress_current"] = event["videos_processed"]
        elif "current" in event:
            fields["progress_current"] = event["current"]

        if "error" in event and event["error"] is not None:
            fields["error"] = str(event["error"])

        if fields:
            try:
                jobs.update(job_id, **fields)
            except Exception:  # noqa: BLE001
                _log.exception("on_progress update failed for %s", job_id)

    return _cb


def _poll_pipeline_jobs(
    db_path: Path,
    jobs: ImportJobsStore,
    job_id: str,
    stop: threading.Event,
) -> None:
    """Sample ``pipeline_jobs`` until the import row is terminal.

    Until data-pipeline wires ``on_progress``, this is the only signal we
    have on per-video progress. The orchestrator creates ``pipeline_jobs``
    only after parse_export completes, so empty results in the first few
    polls are expected.
    """
    while not stop.is_set():
        try:
            counts = _read_pipeline_counts(db_path)
        except sqlite3.OperationalError:
            counts = None
        if counts is not None:
            total, processed, phase = counts
            fields: dict[str, Any] = {
                "progress_total": total,
                "progress_current": processed,
            }
            if phase is not None:
                fields["progress_phase"] = phase
            try:
                jobs.update(job_id, **fields)
            except Exception:  # noqa: BLE001
                pass
        stop.wait(_POLL_INTERVAL_S)


def _read_pipeline_counts(
    db_path: Path,
) -> tuple[int, int, str | None] | None:
    """(total, processed, phase) from data-pipeline's table.

    Returns None when ``pipeline_jobs`` doesn't exist yet (orchestrator
    hasn't initialised its schema).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        # Probe for the table without raising on first call before schema
        # init lands.
        exists = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'pipeline_jobs'"
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM pipeline_jobs GROUP BY status"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None
    by_status = {r["status"]: r["n"] for r in rows}
    total = sum(by_status.values())
    processed = by_status.get("complete", 0)
    phase: str | None = None
    for src in ("preprocessed", "downloaded", "queued", "failed"):
        if by_status.get(src, 0):
            phase = _PIPELINE_TO_PHASE.get(src)
            break
    return total, processed, phase


# ---------------------------------------------------------------------------
# Default orchestrator factory
# ---------------------------------------------------------------------------

def default_orchestrator_factory(
    *,
    db_path: Path,
    activations_dir: Path,
    videos_dir: Path,
    processed_dir: Path,
    mode: ImportMode,
    on_progress: ProgressCallback,
) -> Any:
    """Construct a real ``neural_media_pipeline.Orchestrator``.

    ``mode="mock"`` (the demo path) skips both yt-dlp and ffmpeg — only
    parsing + mock inference run, so the catalog populates in seconds
    against the user's actual TikTok export without any network round
    trip. Probes the Orchestrator signature for ``on_progress=`` and
    silently drops it if older checkouts don't accept it (the poller
    thread provides a fallback signal there).
    """
    from neural_media_pipeline import Orchestrator, OrchestratorConfig

    cfg_kwargs: dict[str, Any] = {
        "db_path": db_path,
        "videos_dir": videos_dir,
        "processed_dir": processed_dir,
        "activations_dir": activations_dir,
    }

    # Pass mock-mode skip flags only if the pipeline version we're
    # linked against supports them. data-pipeline shipped these in
    # round 3; the probe keeps older worktrees buildable.
    cfg_field_names = _orchestrator_config_field_names()
    if mode == "mock":
        if "skip_download" in cfg_field_names:
            cfg_kwargs["skip_download"] = True
        if "skip_preprocess" in cfg_field_names:
            cfg_kwargs["skip_preprocess"] = True

    cfg = OrchestratorConfig(**cfg_kwargs)

    kwargs: dict[str, Any] = {}
    if mode == "real":
        kwargs["inference_fn"] = _build_real_inference_fn()

    if _orchestrator_accepts_on_progress():
        kwargs["on_progress"] = on_progress

    return Orchestrator(cfg, **kwargs)


def _orchestrator_config_field_names() -> frozenset[str]:
    try:
        from neural_media_pipeline import OrchestratorConfig
    except ImportError:
        return frozenset()
    # Works for both dataclasses (the round-3 implementation) and
    # Pydantic models — both expose field metadata.
    fields = getattr(OrchestratorConfig, "__dataclass_fields__", None)
    if fields is not None:
        return frozenset(fields)
    model_fields = getattr(OrchestratorConfig, "model_fields", None)
    if model_fields is not None:
        return frozenset(model_fields)
    return frozenset()


def _orchestrator_accepts_on_progress() -> bool:
    try:
        from neural_media_pipeline import Orchestrator
    except ImportError:
        return False
    try:
        sig = inspect.signature(Orchestrator.__init__)
    except (TypeError, ValueError):
        return False
    return "on_progress" in sig.parameters


def _build_real_inference_fn() -> Callable[..., Any]:
    """Construct a ``run_inference`` wrapper bound to TribeBackend.

    Raises ImportError (which the route handler turns into 400) when the
    ``[real]`` extra isn't installed.
    """
    try:
        from neural_media_inference import TribeBackend, run_inference
    except ImportError as exc:  # pragma: no cover — env-dependent
        raise ImportError(
            "real mode requires the [real] inference extra: "
            "pip install 'neural-media-inference[real]'"
        ) from exc

    backend = TribeBackend(accept_licenses=True)

    def _run(**kwargs: Any) -> Any:
        return run_inference(backend=backend, **kwargs)

    return _run


def real_mode_available() -> bool:
    """Cheap precheck used by POST /import to 400 early.

    ``TribeBackend`` is exposed as a lazy attribute and always imports
    cleanly even without the ``[real]`` extra. The real signal that the
    extra is installed is that ``torch`` is importable — it's the
    heaviest direct dep and the one the extra exists to pull in.
    """
    try:
        import importlib
        importlib.import_module("torch")
    except ImportError:
        return False
    try:
        from neural_media_inference import TribeBackend  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "ImportJob",
    "ImportJobProgress",
    "ImportJobStatus",
    "ImportJobsStore",
    "ImportMode",
    "ImportRunner",
    "JobAlreadyRunning",
    "default_orchestrator_factory",
    "real_mode_available",
]
