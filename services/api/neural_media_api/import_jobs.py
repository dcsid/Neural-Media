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
import shutil
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

# Capability-blocker tokens. Frontend renders different copy per code; keep
# in sync with the docstring on ``real_mode_capabilities`` and the
# capabilities endpoint in ``main.py``.
CAP_MISSING_EXTRA = "missing-extra"
CAP_MISSING_GPU = "missing-gpu"
CAP_MISSING_FFMPEG = "missing-ffmpeg"
CAP_MISSING_YT_DLP = "missing-yt-dlp"

# Order matters: most-fundamental first. POST /import surfaces the first
# blocker in this priority order as the structured error_code.
CAPABILITY_BLOCKER_PRIORITY: tuple[str, ...] = (
    CAP_MISSING_EXTRA,
    CAP_MISSING_FFMPEG,
    CAP_MISSING_YT_DLP,
    CAP_MISSING_GPU,
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

    def latest(self) -> ImportJob | None:
        """Most-recently-created job, regardless of status.

        Used by the /debug endpoint to surface the latest import for
        diagnostics. Returns None if the table is empty.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM import_jobs "
                "ORDER BY created_at DESC LIMIT 1"
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


class JobNotFound(LookupError):
    """Retry referenced an id that doesn't exist."""


class JobNotRetryable(RuntimeError):
    """Retry referenced a job that isn't in a terminal-failure state.

    Only ``failed`` and ``partial`` jobs may be retried — ``queued`` /
    ``running`` aren't terminal and ``complete`` has nothing to retry.
    """

    def __init__(self, job: ImportJob) -> None:
        super().__init__(
            f"import job {job.id} has status {job.status!r}; "
            "only 'failed' or 'partial' jobs can be retried"
        )
        self.job = job


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
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> ImportJob:
        """Register a queued job and start its worker thread.

        Raises ``JobAlreadyRunning`` if another job is in flight.

        ``since`` / ``until`` (both UTC) are forwarded to the orchestrator
        to cap the events the importer pulls out of the export — see
        ``parse_export``'s docstring for the window semantics.
        """
        job = self._claim_slot(mode=mode, source_filename=source_filename)
        _log.info(
            "event=import_submitted op=ingest job_id=%s mode=%s "
            "source_filename=%s since=%s until=%s",
            job.id, mode, source_filename or "",
            since.isoformat() if since else "",
            until.isoformat() if until else "",
        )
        thread = threading.Thread(
            target=self._run,
            kwargs=dict(
                job_id=job.id,
                mode=mode,
                op="ingest",
                export_path=export_path,
                since=since,
                until=until,
            ),
            daemon=True,
            name=f"import-{job.id[:8]}",
        )
        thread.start()
        return job

    def submit_retry(self, prev_job_id: str) -> ImportJob:
        """Spawn a new job that drives ``run_pending`` instead of re-parsing.

        Reuses the previous job's mode + ``source_filename`` so the UI can
        show "retry of <filename>". Raises:

        - ``JobNotFound`` if the id is unknown,
        - ``JobNotRetryable`` if status isn't ``failed`` or ``partial``,
        - ``JobAlreadyRunning`` if another job is in flight.
        """
        prev = self._jobs.get(prev_job_id)
        if prev is None:
            raise JobNotFound(prev_job_id)
        if prev.status not in ("failed", "partial"):
            raise JobNotRetryable(prev)

        job = self._claim_slot(
            mode=prev.mode,
            source_filename=prev.source_filename,
        )
        _log.info(
            "event=import_submitted op=retry job_id=%s prev_job_id=%s "
            "mode=%s source_filename=%s",
            job.id, prev_job_id, prev.mode, prev.source_filename or "",
        )
        thread = threading.Thread(
            target=self._run,
            kwargs=dict(
                job_id=job.id,
                mode=prev.mode,
                op="retry",
            ),
            daemon=True,
            name=f"retry-{job.id[:8]}",
        )
        thread.start()
        return job

    # --- internals -------------------------------------------------------

    def _claim_slot(
        self,
        *,
        mode: ImportMode,
        source_filename: str | None,
    ) -> ImportJob:
        """Atomically reserve the singleton slot, create the row, return it."""
        with self._lock:
            # Race-free by construction: `_running_job_id` is read and
            # written ONLY under `self._lock` — here, and in `_run`'s
            # finally block when a finishing job releases the slot. The lock
            # serializes the whole get()+status-check+create as one atomic
            # step, so two concurrent claimers can never both pass the gate.
            # There is no TOCTOU window between `jobs.get()` and the status
            # check either: both read the single snapshot taken here.
            #
            # Consulting the DB row (not just `_running_job_id`) is a
            # deliberate refinement: a worker stamps its terminal status
            # into SQLite a beat before it reacquires the lock to clear the
            # slot. Treating an already-terminal row as a stale slot lets a
            # fresh claim proceed in that window instead of returning a
            # spurious 409.
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
        return job

    def _run(
        self,
        *,
        job_id: str,
        mode: ImportMode,
        op: str,                                  # "ingest" | "retry"
        export_path: Path | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> None:
        """Background-thread entry point. Owns one orchestrator lifetime."""
        poller_stop = threading.Event()
        poller: threading.Thread | None = None
        orch = None
        try:
            # Flip the top-level status once; phase tells the story from here.
            self._jobs.update(
                job_id,
                status="running",
                # Retry starts in the same parsing-like "warming up" state
                # for UI consistency; the poller flips it the moment the
                # pipeline starts emitting per-video stages.
                progress_phase="parsing",
            )

            cb = _progress_callback(self._jobs, job_id)
            orch = self._factory(
                db_path=self.db_path,
                activations_dir=self.activations_dir,
                videos_dir=self.videos_dir,
                processed_dir=self.processed_dir,
                mode=mode,
                on_progress=cb,
            )

            # The on_progress callback only fires if data-pipeline wired
            # support for it via the constructor. Polling pipeline_jobs
            # covers the gap.
            poller = threading.Thread(
                target=_poll_pipeline_jobs,
                args=(self.db_path, self._jobs, job_id, poller_stop),
                daemon=True,
                name=f"import-poll-{job_id[:8]}",
            )
            poller.start()

            if op == "retry":
                summary = _call_run_pending(orch, progress=cb)
            else:
                assert export_path is not None, "ingest op requires export_path"
                summary = _call_ingest_export(
                    orch, export_path, since=since, until=until,
                )

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
    """Build the ``on_progress`` callback we hand to the Orchestrator.

    The returned callable takes a single free-form ``dict`` event and
    patches the import_jobs row. It tolerates schema drift: unrecognised
    keys are ignored and a failed DB write is logged, never raised, so a
    progress hiccup can't crash the orchestrator thread.

    Recognised keys (all optional):

    - ``phase`` or ``stage`` (str) → ``progress_phase``. Callers should use
      the ``ImportPhase`` vocabulary from ``shared.schemas``
      (``parsing | downloading | preprocessing | inferring``); the value is
      stored verbatim and not validated here.
    - ``videos_total`` or ``total`` (int) → ``progress_total``.
    - ``videos_processed`` or ``current`` (int) → ``progress_current``.
    - ``error`` (str, when non-None) → ``error``.

    Flat (``videos_*``) and canonical (``current``/``total``) spellings are
    both accepted; the flat names win when both appear. None of these
    touch the top-level job ``status`` — that only flips at submission and
    at terminal (see ``ImportRunner._run``).
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

    ``mode="mock"`` → ``skip_download=True, skip_preprocess=True`` so the
    orchestrator never touches yt-dlp or ffmpeg; the synthetic per-video
    duration matches the committed sample fixtures. ``mode="real"`` →
    full pipeline + a TribeBackend bound to the real TRIBE v2 weights.

    Probes the Orchestrator signature for ``on_progress=`` and silently
    drops it if data-pipeline hasn't shipped support yet — the poller
    thread provides a fallback signal in that case.
    """
    from neural_media_pipeline import Orchestrator, OrchestratorConfig

    cfg_kwargs: dict[str, Any] = dict(
        db_path=db_path,
        videos_dir=videos_dir,
        processed_dir=processed_dir,
        activations_dir=activations_dir,
    )
    if mode == "mock":
        cfg_kwargs["skip_download"] = True
        cfg_kwargs["skip_preprocess"] = True
    cfg = OrchestratorConfig(**cfg_kwargs)

    init_kwargs: dict[str, Any] = {}
    if mode == "real":
        # Real backend goes through inference_fn — the OrchestratorConfig
        # also accepts `backend=` but threading it through inference_fn
        # keeps the api side in control of how the backend is constructed
        # (license acceptance, weight resolution, etc.).
        init_kwargs["inference_fn"] = _build_real_inference_fn()

    if _orchestrator_accepts_on_progress():
        init_kwargs["on_progress"] = on_progress

    return Orchestrator(cfg, **init_kwargs)


def _call_run_pending(orch: Any, *, progress: ProgressCallback) -> Any:
    """Invoke ``orch.run_pending`` with the ``progress=`` kwarg when supported.

    Stubs in the test suite don't accept a ``progress`` kwarg, and an
    older real orchestrator may not either — fall back to the bare call
    in that case. The poller thread already provides a progress signal
    regardless.
    """
    try:
        sig = inspect.signature(orch.run_pending)
    except (TypeError, ValueError):
        sig = None

    if sig is not None and "progress" in sig.parameters:
        return orch.run_pending(progress=progress)
    return orch.run_pending()


def _call_ingest_export(
    orch: Any,
    export_path: Path,
    *,
    since: datetime | None,
    until: datetime | None,
) -> Any:
    """Invoke ``orch.ingest_export`` with date-window kwargs when supported.

    The shape we forward depends on the orchestrator's signature: real
    orchestrators (and any stub that takes ``**kwargs``) get the
    keyword args; older stubs that only accept a positional path fall
    back to the legacy call so we don't break them.
    """
    if since is None and until is None:
        return orch.ingest_export(export_path)
    try:
        sig = inspect.signature(orch.ingest_export)
    except (TypeError, ValueError):
        sig = None

    accepts_kwargs = sig is None or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    ) or {"since", "until"}.issubset(sig.parameters.keys() if sig else set())

    if accepts_kwargs:
        return orch.ingest_export(export_path, since=since, until=until)
    return orch.ingest_export(export_path)


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


def real_mode_capabilities() -> tuple[bool, list[str]]:
    """Probe everything the real pipeline needs and report what's missing.

    Returns ``(ok, blockers)`` where ``blockers`` is a subset of
    ``{CAP_MISSING_EXTRA, CAP_MISSING_FFMPEG, CAP_MISSING_YT_DLP,
    CAP_MISSING_GPU}``. ``ok`` is True iff the list is empty.

    Checks, in order:

    - ``torch`` importable AND ``TribeBackend`` re-exported from
      ``neural_media_inference``. Either failure → ``missing-extra``.
    - ``ffmpeg`` on PATH (preprocessor depends on it).
    - ``yt-dlp`` on PATH (downloader depends on it).
    - When torch is present, ``torch.cuda.is_available()``. The real TRIBE
      backend will run on CPU but at glacial pace; the UI uses this to
      warn rather than block.

    Cheap enough to call per-request (no model loading, no subprocesses).
    """
    blockers: list[str] = []

    torch_ok = True
    torch_mod = None
    try:
        import importlib
        torch_mod = importlib.import_module("torch")
    except ImportError:
        torch_ok = False
        blockers.append(CAP_MISSING_EXTRA)

    if torch_ok:
        try:
            from neural_media_inference import TribeBackend  # noqa: F401
        except ImportError:
            blockers.append(CAP_MISSING_EXTRA)

    if shutil.which("ffmpeg") is None:
        blockers.append(CAP_MISSING_FFMPEG)
    if shutil.which("yt-dlp") is None:
        blockers.append(CAP_MISSING_YT_DLP)

    # GPU check only meaningful when torch is actually importable —
    # otherwise the missing-extra blocker already conveys the bigger
    # problem and probing CUDA would just AttributeError.
    if torch_mod is not None:
        try:
            if not bool(torch_mod.cuda.is_available()):
                blockers.append(CAP_MISSING_GPU)
        except Exception:  # noqa: BLE001 — defensive: any torch shape works
            blockers.append(CAP_MISSING_GPU)

    return (not blockers), blockers


def real_mode_available() -> bool:
    """Backwards-compatible wrapper. Kept for callers that only need a bool."""
    ok, _ = real_mode_capabilities()
    return ok


__all__ = [
    "CAPABILITY_BLOCKER_PRIORITY",
    "CAP_MISSING_EXTRA",
    "CAP_MISSING_FFMPEG",
    "CAP_MISSING_GPU",
    "CAP_MISSING_YT_DLP",
    "ImportJob",
    "ImportJobProgress",
    "ImportJobStatus",
    "ImportJobsStore",
    "ImportMode",
    "ImportRunner",
    "JobAlreadyRunning",
    "JobNotFound",
    "JobNotRetryable",
    "default_orchestrator_factory",
    "real_mode_available",
    "real_mode_capabilities",
]
