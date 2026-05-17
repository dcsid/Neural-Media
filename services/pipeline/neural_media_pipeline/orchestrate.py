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
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from neural_media_inference import InferenceBackend, RunArtifacts, run_inference
from neural_media_inference.aggregate import aggregate_region_metrics
from neural_media_inference.backend import MockBackend as _MockBackend
from neural_media_inference.runner import DEFAULT_PREPROCESSING_PARAMS

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


def _chunks(seq: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    """Yield consecutive slices of ``seq`` of length ``size``."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Mock-fast workers (module level for ProcessPoolExecutor picklability)
# ---------------------------------------------------------------------------
#
# The default ``inference_fn=run_inference`` is the only entry point that is
# safe to ship across a process boundary — every test in this package
# injects a closure-based fake, which is not pickleable. The fast path
# (``_drive_mock_fast``) opts in only when the configured ``inference_fn``
# is the module default; that keeps the existing 94 tests on the sequential
# path while production CLI runs (``python -m neural_media_pipeline --mock
# ...``) get the multi-core speed-up.

_WORKER_BACKEND: InferenceBackend | None = None


def _pool_context() -> multiprocessing.context.BaseContext:
    """Pick the cheapest multiprocessing start method available.

    Default ``spawn`` re-imports every module in each worker — on macOS
    / Linux that's a hard 100-200 ms tax per worker, which dominated the
    1000-video bench at 14 workers (spawn 13.9s vs forkserver 6.9s).
    ``forkserver`` keeps a long-lived helper that fork()s on demand, so
    workers inherit pre-imported modules without the safety footguns of
    raw ``fork``. Falls back to whatever default is available on
    platforms that don't ship forkserver (e.g. native Windows).
    """
    available = set(multiprocessing.get_all_start_methods())
    # fork beats forkserver here because forkserver's helper has its own
    # cold Python state — every worker re-imports neural_media_inference
    # via inheritance from a near-empty parent (the helper). Plain fork
    # inherits the *real* parent, so numpy + pydantic + our modules are
    # already resident. Empirically: fork-14w 1000 videos = 7.0s vs
    # forkserver = 12.5s vs spawn = 13.9s. macOS fork is "unsafe" for
    # ObjC/Cocoa frameworks, but neither our workers nor their imports
    # touch those.
    for preferred in ("fork", "forkserver"):
        if preferred in available:
            return multiprocessing.get_context(preferred)
    return multiprocessing.get_context()  # spawn fallback


def _mock_worker_init() -> None:
    """Build the MockBackend once per worker process.

    ``np.random.default_rng`` is cheap but the backend's ``model_id`` /
    ``model_version`` are class-level constants, so this is mostly a hook
    for future backends that have heavier setup (e.g. weights load).
    """
    global _WORKER_BACKEND
    _WORKER_BACKEND = _MockBackend()


def _lean_config_hash(
    *, model_id: str, model_version: str, seed: int, params: dict[str, Any],
) -> str:
    """Replicates ``runner._config_hash`` without importing the private symbol.

    Same JSON-canonicalisation + SHA-256 the runner uses, so two videos
    that produce the same hash here would produce the same hash via
    ``run_inference``. We replicate (rather than import) because the
    runner's helper is private and out of this worker's scope to refactor.
    """
    payload = json.dumps(
        {"model_id": model_id, "model_version": model_version,
         "seed": seed, "params": params},
        sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mock_worker_run_lean(
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | str]:
    """Inference + row-building with NO disk I/O.

    Used in the ``purge_activations=True`` branch of the mock-fast path.
    The full ``run_inference`` would write a ``(T, 20484)`` fp32 ``.npz``
    and a JSON sidecar that the orchestrator then *immediately unlinks*;
    skipping those writes drops 14-worker contention on the activations
    directory and is the single biggest win for ``--mock
    --purge-after-inference`` (the demo path).

    What we keep doing: backend.infer + aggregate_region_metrics + the
    reproducibility envelope (model id, version, seed, params_json with
    config_hash). The SQLite rows that land are byte-equal to the
    full-runner path except for ``activation_path``, which would have
    been the npz path the orchestrator was about to delete anyway — we
    seed it with the same conventional name (``<activations_dir>/{run_id}.npz``)
    so a downstream check that doesn't know the file was never written
    sees a consistent shape.
    """
    video_id = args["video_id"]
    try:
        backend: InferenceBackend = _WORKER_BACKEND  # set by init
        duration_s = float(args["duration_s"])
        sample_rate_hz = float(args["sample_rate_hz"])
        seed = int(args["seed"])
        activations = backend.infer(
            video_id=video_id, duration_s=duration_s,
            seed=seed, sample_rate_hz=sample_rate_hz,
        )
        num_timepoints = int(activations.shape[0])

        run_id = _uuid_for_run()
        activations_dir = Path(args["activations_dir"])
        # Conventional path even though nothing lands on disk — the
        # orchestrator will set activation_path='' on the purge sweep.
        activation_path = str(activations_dir / f"{run_id}.npz")

        preprocessing_params = {
            **DEFAULT_PREPROCESSING_PARAMS, **(args.get("extra_params") or {}),
        }
        created_at = datetime.now(timezone.utc)
        params_json: dict[str, Any] = {
            "duration_s": duration_s,
            "sample_rate_hz": sample_rate_hz,
            "num_timepoints": num_timepoints,
            "preprocessing": preprocessing_params,
            "created_at_utc": created_at.isoformat(),
        }
        params_json["config_hash"] = _lean_config_hash(
            model_id=backend.model_id, model_version=backend.model_version,
            seed=seed, params=params_json,
        )

        metric_dicts = aggregate_region_metrics(
            activations, video_id=video_id, inference_run_id=run_id,
        )

        return (video_id, {
            "run_row": (
                run_id, video_id, backend.model_id, backend.model_version,
                seed, json.dumps(params_json), created_at.isoformat(),
                activation_path, "complete",
            ),
            "metric_rows": [
                (m["inference_run_id"], m["region_id"], m["video_id"],
                 m["mean"], m["peak"], m["sustained"],
                 json.dumps(m["timeseries"]))
                for m in metric_dicts
            ],
            "duration_s": duration_s,
            "activation_path": activation_path,
            "sidecar_path": str(activations_dir / f"{run_id}.meta.json"),
            "inference_run_id": run_id,
        })
    except Exception as exc:  # noqa: BLE001 — boundary swallow
        return (video_id, f"{type(exc).__name__}: {exc}")


def _uuid_for_run() -> str:
    """uuid4 via uuid; module-level so workers don't re-import per task."""
    import uuid
    return str(uuid.uuid4())


def _mock_worker_run(
    args: dict[str, Any],
) -> tuple[str, dict[str, Any] | str]:
    """Run one inference in the worker. Returns ``(video_id, result)``.

    Why we return SQLite-ready tuples (not RunArtifacts):
    pickling a full ``RunArtifacts`` back to the parent is ~1 MB/video
    because ``ActivationOutput.keyframe_vertices`` is 5 keyframes ×
    20 484 floats. On a 1000-video run that's ~1 GB of inter-process
    traffic — empirically the parallel path was 2-4× *slower* than the
    sequential one until we collapsed the return payload to the rows
    we actually need to insert.

    All side effects (npz, meta.json, optional payload JSON) happen in
    the worker process; the parent only does the bulk SQLite writes.

    On failure: returns ``(video_id, error_string)``. The worker
    boundary must not leak exceptions — the parent aggregates failures
    into the IngestSummary exactly like the sequential path does.
    """
    video_id = args["video_id"]
    try:
        kw = {
            "video_id": video_id,
            "duration_s": args["duration_s"],
            "seed": args["seed"],
            "sample_rate_hz": args["sample_rate_hz"],
            "activations_dir": args["activations_dir"],
            "extra_params": args["extra_params"],
            "backend": _WORKER_BACKEND,
        }
        artifacts = run_inference(**kw)
        # Persist the downsampled payload JSON next to the npz only when
        # purge_activations is off — same gate the orchestrator applies
        # in the sequential path. Doing it here keeps the (large)
        # ActivationOutput out of the pickled return value.
        if not args["purge_activations"]:
            payload_path = (
                Path(args["activations_dir"])
                / f"{artifacts.inference_run.id}.json"
            )
            payload_path.write_text(artifacts.activation_payload.model_dump_json())
        run = artifacts.inference_run
        return (video_id, {
            "run_row": (
                run.id, run.video_id, run.model_id, run.model_version,
                run.seed, json.dumps(run.params_json),
                run.created_at.isoformat(), run.activation_path, run.status,
            ),
            "metric_rows": [
                (m.inference_run_id, m.region_id, m.video_id,
                 m.mean, m.peak, m.sustained, json.dumps(m.timeseries))
                for m in artifacts.region_metrics
            ],
            "duration_s": args["duration_s"],
            "activation_path": str(artifacts.activation_path),
            "sidecar_path": str(artifacts.sidecar_path),
            "inference_run_id": run.id,
        })
    except Exception as exc:  # noqa: BLE001 — boundary swallow, see above
        return (video_id, f"{type(exc).__name__}: {exc}")


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
    # When True, delete the raw + preprocessed mp4s once the inference
    # run is persisted. Keeps peak transient disk to ~10 MB even on a
    # multi-thousand-video ingest, at the cost of the yt-dlp dedup
    # cache — a re-run has to hit TikTok again. Off by default so dev
    # iteration on a small fixture doesn't pay the redownload tax.
    purge_after_inference: bool = False
    # Tier-b cleanup: after the inference run is persisted, also drop
    # the raw .npz, the .meta.json sidecar, and the downsampled JSON
    # payload sitting beside them. The region_metrics rows in SQLite
    # survive — that's the irreducible product. /v/[id] loses its
    # per-vertex playback for purged videos; brain-viz has a graceful
    # fallback for that. Independent of purge_after_inference: a caller
    # can keep mp4s but drop activations, or vice versa.
    purge_activations: bool = False
    # Mock-mode parallelism cap. ``None`` (default) means "auto": pick
    # ``min(cpu_count, max(1, total // 50))`` so a 5k-video ingest fans
    # out across every core while an 8-video fixture stays single-process
    # (worker startup overhead would dwarf the wins). ``1`` or ``0``
    # forces the sequential path — useful for tests and for reproducing
    # ordering-sensitive bugs. Only honoured when both ``skip_download``
    # and ``skip_preprocess`` are true AND the orchestrator's
    # ``inference_fn`` is the module-default ``run_inference`` (the only
    # one we can pickle across the process boundary).
    parallel_workers: int | None = None

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
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> IngestSummary:
        """Parse export → upsert → drive every pending job. Emits progress.

        This is the canonical programmatic entry point — the in-process
        equivalent of ``python -m neural_media_pipeline ...``. The api
        worker calls it from a background thread off ``/api/v1/import``.

        ``progress`` is invoked at every phase boundary and once more
        after each video completes inference (with an incremented
        ``videos_processed``). The callback is synchronous — the api
        worker is responsible for queueing/serialising if it cares.

        ``since`` / ``until`` (both UTC) cap the events parsed out of the
        export to a half-open ``[since, until)`` window. Use this to
        avoid pointing the orchestrator at a multi-year history — TRIBE
        v2 inference is the expensive step, not the parse.
        """
        cb: ProgressCallback = progress or (lambda _e: None)

        cb(ProgressEvent(phase="parsing", message="parsing export"))
        videos, events = parse_export(export_path, since=since, until=until)
        self._upsert_videos(videos)
        self._upsert_watch_events(events)
        queued = self._enqueue_pending(videos)

        summary = self._drive_all(cb)
        summary.parsed_videos = len(videos)
        summary.parsed_events = len(events)
        summary.queued = queued
        return summary

    # Back-compat name: thin wrapper around `run`.
    def ingest_export(
        self,
        export_path: Path,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> IngestSummary:
        return self.run(export_path, since=since, until=until)

    def run_pending(
        self, *, progress: ProgressCallback | None = None,
    ) -> IngestSummary:
        """Drive every non-complete job without re-parsing an export."""
        cb: ProgressCallback = progress or (lambda _e: None)
        return self._drive_all(cb)

    # --- inner loop ------------------------------------------------------

    def _drive_all(self, cb: ProgressCallback) -> IngestSummary:
        pending = self._pending_job_ids()
        total = len(pending)
        if total == 0:
            return IngestSummary()

        # Mock-mode fast path: bypass the per-video DB chatter, parallelise
        # the inference loop, and batch SQLite writes at the end. Only
        # safe when the inference_fn is the picklable module default —
        # closures (every test in this package) fall through to the
        # sequential path below.
        if self._fast_path_available():
            return self._drive_mock_fast(cb, pending)

        # Emit the "switching out of parsing" event with totals known.
        first_phase: Phase = (
            "inferring" if self.cfg.skip_download and self.cfg.skip_preprocess
            else "preprocessing" if self.cfg.skip_download
            else "downloading"
        )
        cb(ProgressEvent(phase=first_phase, videos_total=total, videos_processed=0))

        # Coalesce per-video progress at large totals — the api worker
        # writes the dashboard row on every event and the UI can only
        # render ~200 distinct positions on a progress bar anyway. For
        # the 8-video fixture every event still fires (every == 1).
        every = max(1, total // 200)

        summary = IngestSummary()
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
            # worker uses this to update its job-status row. Coalesced
            # at large totals but the last event always fires so the UI
            # converges on 100%.
            if processed % every == 0 or processed == total:
                cb(ProgressEvent(
                    phase="inferring",
                    videos_total=total,
                    videos_processed=processed,
                    message=video_id,
                ))
        return summary

    def _fast_path_available(self) -> bool:
        """Whether the mock-fast path can take over for this run.

        Three conditions:
          * Both skip flags set (i.e. mock/demo mode — no yt-dlp, no
            ffmpeg).
          * The inference callable is the module-default ``run_inference``
            (the only one we can ship to a worker process; closures fail
            to pickle).
          * The user has not explicitly capped workers at 1 (the opt-out
            for tests that want the sequential path even in mock mode).
        """
        if not (self.cfg.skip_download and self.cfg.skip_preprocess):
            return False
        if self._infer is not run_inference:
            return False
        if self.cfg.parallel_workers is not None and self.cfg.parallel_workers <= 1:
            return False
        return True

    # --- mock-mode fast path --------------------------------------------

    def _drive_mock_fast(
        self,
        cb: ProgressCallback,
        pending: list[str],
    ) -> IngestSummary:
        """Bulk + parallel mock-mode driver. Replaces ``_drive_all`` when
        ``_fast_path_available()`` returns True.

        Contract preservation:
          * Same SQLite rows land (videos.duration_s, inference_runs,
            region_metrics, pipeline_jobs.status='complete').
          * Same artifacts on disk when ``purge_activations`` is off
            (npz + meta.json + payload JSON next to them).
          * Same purge semantics when either purge flag is on.
          * Progress events: one initial "totals known" tick, then
            ``min(200, total)`` per-video ticks coalesced across
            completions, then a final tick at 100%.

        Failure semantics: each worker swallows exceptions and returns
        an error string; the parent flips the corresponding job to
        FAILED via the same code path as the sequential driver.
        """
        total = len(pending)
        summary = IngestSummary()

        cb(ProgressEvent(phase="inferring", videos_total=total, videos_processed=0))
        every = max(1, total // 200)

        videos = self._load_videos_by_ids(pending)
        # Bump attempts up-front for every pending job — matches the
        # sequential path's bump-then-execute order.
        self._bump_attempts_bulk([v.id for v in videos])

        args_by_id: dict[str, dict[str, Any]] = {}
        sample_rate_hz = self.cfg.fmri_sample_rate_hz
        extra_params = self.preprocess_cfg.extra_params()
        purge_activations = self.cfg.purge_activations
        for v in videos:
            duration_s = self._resolve_duration_s(v)
            args_by_id[v.id] = {
                "video_id": v.id,
                "duration_s": duration_s,
                "seed": _seed_for(v.id),
                "sample_rate_hz": sample_rate_hz,
                "activations_dir": self.cfg.activations_dir,
                "extra_params": extra_params,
                "purge_activations": purge_activations,
            }

        n_workers = self._resolve_worker_count(total)
        completed: list[dict[str, Any]] = []
        failed: list[tuple[str, str]] = []

        def _record_result(video_id: str, result: dict[str, Any] | str) -> None:
            if isinstance(result, dict):
                completed.append(result)
            else:
                failed.append((video_id, result))

        processed = 0
        last_video_id: str | None = None

        # Worker selection: the lean worker skips ``run_inference``'s
        # ``np.savez`` + sidecar write entirely, which under 14 parallel
        # workers was contending on the activations directory and
        # turning a CPU-bound problem into a disk-bound one. Safe only
        # when the disk artifacts would be unlinked moments later —
        # i.e. ``purge_activations=True``. Otherwise we go through the
        # full runner so the npz + sidecar land on disk for /v/[id].
        worker_fn = (
            _mock_worker_run_lean if purge_activations else _mock_worker_run
        )

        args_list = list(args_by_id.values())
        if n_workers <= 1:
            # Sequential mock-fast: still bypasses per-video DB chatter
            # and avoids process-pool startup on small ingests.
            _mock_worker_init()
            for args in args_list:
                video_id, result = worker_fn(args)
                _record_result(video_id, result)
                processed += 1
                last_video_id = video_id
                if processed % every == 0 or processed == total:
                    cb(ProgressEvent(
                        phase="inferring", videos_total=total,
                        videos_processed=processed, message=video_id,
                    ))
        else:
            # forkserver beats spawn by 2× on macOS / Linux because each
            # worker doesn't re-import numpy + pydantic + neural_media_*
            # from scratch — the bench script proved spawn-14w on 1000
            # videos was *slower* than sequential mock-fast, and switching
            # to forkserver flipped it to a clean 1.6× speedup. Windows
            # falls back to the default (spawn) since forkserver isn't
            # available there.
            mp_ctx = _pool_context()
            with ProcessPoolExecutor(
                max_workers=n_workers, initializer=_mock_worker_init,
                mp_context=mp_ctx,
            ) as pool:
                # chunksize tuned for the typical task size — each call
                # is ~10 ms of MockBackend.infer + a few ms of npz +
                # JSON I/O. Empirically (see bench_mock.py log) small
                # chunks beat large ones for these short tasks: 1000
                # videos × 14 workers ran 7.8s at chunksize=17 vs 6.7s
                # at chunksize=1, because tail-stragglers dominate the
                # wall time. Cap at 4 to keep the queue lock from
                # bottlenecking on very large ingests.
                chunksize = max(1, min(4, total // (n_workers * 16) or 1))
                for video_id, result in pool.map(
                    worker_fn, args_list, chunksize=chunksize,
                ):
                    _record_result(video_id, result)
                    processed += 1
                    last_video_id = video_id
                    if processed % every == 0 or processed == total:
                        cb(ProgressEvent(
                            phase="inferring", videos_total=total,
                            videos_processed=processed, message=video_id,
                        ))

        # Bulk persistence — one transaction for runs + region_metrics,
        # one for the job-status updates, one for the duration backfill.
        self._persist_runs_bulk_from_rows(completed)
        self._update_jobs_bulk_from_rows(completed=completed, failed=failed)
        self._backfill_durations_bulk_from_rows(completed)

        # Side-effect work that doesn't fit in a bulk write: optional
        # post-inference cleanup of mp4s and/or activation blobs.
        if self.cfg.purge_after_inference:
            self._purge_video_artifacts_bulk(
                [row["run_row"][1] for row in completed]  # row[1] = video_id
            )
        if purge_activations:
            self._purge_activation_artifacts_bulk_from_rows(completed)

        # Final tick if the coalescer skipped it.
        if processed > 0 and processed % every != 0 and last_video_id is not None:
            cb(ProgressEvent(
                phase="inferring", videos_total=total,
                videos_processed=processed, message=last_video_id,
            ))

        summary.completed = len(completed)
        summary.failed = len(failed)
        summary.errors.extend(failed)
        return summary

    def _resolve_worker_count(self, total: int) -> int:
        """Pick the number of worker processes for one mock-fast run.

        Honours an explicit ``parallel_workers`` config override. Auto
        formula = ``min(cpu_count, max(1, total // 50))``: small ingests
        stay sequential (startup tax) and large ingests spread across
        every available core.
        """
        if self.cfg.parallel_workers is not None:
            return max(1, self.cfg.parallel_workers)
        cpu = os.cpu_count() or 1
        return min(cpu, max(1, total // 50))

    def _load_videos_by_ids(self, ids: list[str]) -> list[VideoMetadata]:
        """Bulk fetch of VideoMetadata for the mock-fast path.

        One SELECT per IN-chunk instead of N round-trips through
        ``_load_video``. Returns rows in the input id order so progress
        callbacks see a stable sequence.
        """
        if not ids:
            return []
        by_id: dict[str, VideoMetadata] = {}
        for chunk in _chunks(ids, 500):
            placeholders = ",".join("?" * len(chunk))
            cur = self._conn.execute(
                f"""
                SELECT id, source_url, title, author, duration_s,
                       downloaded, local_path, tags_json
                  FROM videos WHERE id IN ({placeholders})
                """,
                chunk,
            )
            for row in cur.fetchall():
                by_id[row[0]] = VideoMetadata(
                    id=row[0], source_url=row[1], title=row[2],
                    author=row[3], duration_s=row[4],
                    downloaded=bool(row[5]), local_path=row[6],
                    tags=json.loads(row[7]),
                )
        return [by_id[i] for i in ids if i in by_id]

    def _bump_attempts_bulk(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._conn:
            self._conn.executemany(
                "UPDATE pipeline_jobs SET attempts = attempts + 1 WHERE video_id = ?",
                [(vid,) for vid in ids],
            )

    def _persist_runs_bulk_from_rows(
        self, completed: list[dict[str, Any]],
    ) -> None:
        """Bulk inference_runs + region_metrics inserts from worker rows.

        Workers pre-serialise their results into SQLite-ready tuples to
        keep the inter-process payload small (~1 MB → ~5 KB per video).
        This consumes those tuples directly.
        """
        if not completed:
            return
        run_rows = [row["run_row"] for row in completed]
        metric_rows: list[tuple] = []
        for row in completed:
            metric_rows.extend(row["metric_rows"])
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO inference_runs
                    (id, video_id, model_id, model_version, seed,
                     params_json, created_at, activation_path, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                run_rows,
            )
            if metric_rows:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO region_metrics
                        (inference_run_id, region_id, video_id,
                         mean, peak, sustained, timeseries_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    metric_rows,
                )

    def _update_jobs_bulk_from_rows(
        self,
        *,
        completed: list[dict[str, Any]],
        failed: list[tuple[str, str]],
    ) -> None:
        """Bulk pipeline_jobs status update for the mock-fast path.

        Mirrors the per-video updates the sequential ``_drive_one`` would
        have issued (status, inference_run_id, last_error). Skips writes
        when there's nothing to update so the WAL doesn't churn.
        """
        if not completed and not failed:
            return
        now = _utcnow_iso()
        with self._conn:
            if completed:
                self._conn.executemany(
                    """
                    UPDATE pipeline_jobs
                       SET status = ?, inference_run_id = ?,
                           last_error = NULL, updated_at = ?
                     WHERE video_id = ?
                    """,
                    [
                        (STATUS_COMPLETE, row["inference_run_id"], now,
                         row["run_row"][1])
                        for row in completed
                    ],
                )
            if failed:
                self._conn.executemany(
                    """
                    UPDATE pipeline_jobs
                       SET status = ?, last_error = ?, updated_at = ?
                     WHERE video_id = ?
                    """,
                    [(STATUS_FAILED, err, now, vid) for vid, err in failed],
                )

    def _backfill_durations_bulk_from_rows(
        self, completed: list[dict[str, Any]],
    ) -> None:
        """Write probed durations back to ``videos.duration_s`` in one shot.

        Same WHERE-guard as the sequential path so we never clobber a
        non-zero duration that arrived from a real ffprobe run.
        """
        if not completed:
            return
        rows = [
            (row["duration_s"], row["run_row"][1])  # row[1] = video_id
            for row in completed
        ]
        with self._conn:
            self._conn.executemany(
                "UPDATE videos SET duration_s = ? WHERE id = ? AND duration_s <= 0",
                rows,
            )

    def _purge_activation_artifacts_bulk_from_rows(
        self, completed: list[dict[str, Any]],
    ) -> None:
        """Mock-fast variant of ``_purge_activation_artifacts_bulk``.

        Takes worker-emitted row dicts (npz path, sidecar path, run id)
        instead of full RunArtifacts. Same swallow-and-log semantics as
        the per-video sibling.
        """
        if not completed:
            return
        for row in completed:
            paths_to_drop = [
                Path(row["activation_path"]),
                Path(row["sidecar_path"]),
                Path(row["activation_path"]).with_suffix(".json"),
            ]
            video_id = row["run_row"][1]
            for path in paths_to_drop:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    _log.warning(
                        "activation purge failed for %s: %s", video_id, exc,
                    )
        with self._conn:
            self._conn.executemany(
                "UPDATE inference_runs SET activation_path = '' WHERE id = ?",
                [(row["inference_run_id"],) for row in completed],
            )

    def _purge_video_artifacts_bulk(self, ids: list[str]) -> None:
        """Bulk variant of ``_purge_video_artifacts``.

        Filesystem unlinks remain per-video (the OS gives us no batch
        primitive) but the catalog row clears collapse into one
        executemany.
        """
        if not ids:
            return
        _log.info("event=cleanup_started kind=videos count=%d", len(ids))
        for video_id in ids:
            for path in (
                self.cfg.videos_dir / f"{video_id}.mp4",
                self.cfg.processed_dir / f"{video_id}.mp4",
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    _log.warning("purge failed for %s: %s", video_id, exc)
        with self._conn:
            self._conn.executemany(
                "UPDATE videos SET downloaded = 0, local_path = NULL WHERE id = ?",
                [(vid,) for vid in ids],
            )

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
        rows = [
            (v.id, v.source_url, v.title, v.author, v.duration_s,
             1 if v.downloaded else 0, v.local_path,
             json.dumps(v.tags), next_idx + i)
            for i, v in enumerate(videos)
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO videos
                    (id, source_url, title, author, duration_s,
                     downloaded, local_path, tags_json, first_seen_idx)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _upsert_watch_events(self, events: list[WatchEvent]) -> None:
        if not events:
            return
        rows = [
            (e.id, e.video_id, e.watched_at.isoformat(),
             e.duration_watched_s, e.completion_pct, e.source)
            for e in events
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO watch_events
                    (id, video_id, watched_at, duration_watched_s,
                     completion_pct, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _enqueue_pending(self, videos: list[VideoMetadata]) -> int:
        """Insert a queued row for each video not already tracked.

        Single round-trip to learn which ids already have a job row, then
        a single executemany insert for the rest. Cuts the per-video
        SELECT chatter that dominated 100k-video runs.
        """
        if not videos:
            return 0
        now = _utcnow_iso()
        ids = [v.id for v in videos]
        existing: set[str] = set()
        # IN-clause batched to stay well under SQLite's default 999
        # parameter ceiling (2026's libsqlite raises it but old wheels
        # bundle older limits).
        for chunk in _chunks(ids, 500):
            placeholders = ",".join("?" * len(chunk))
            cur = self._conn.execute(
                f"SELECT video_id FROM pipeline_jobs WHERE video_id IN ({placeholders})",
                chunk,
            )
            existing.update(row[0] for row in cur.fetchall())
        new_rows = [
            (v.id, STATUS_QUEUED, now) for v in videos if v.id not in existing
        ]
        if new_rows:
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO pipeline_jobs
                        (video_id, status, attempts, updated_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    new_rows,
                )
        return len(new_rows)

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
        # re-decompressing. Skipped under purge_activations because the
        # file would be unlinked moments later (the JSON sidecar was
        # ~30% of the 1000-video mock profile — see docs/bench-results.md).
        payload_path = self.cfg.activations_dir / f"{artifacts.inference_run.id}.json"
        if not self.cfg.purge_activations:
            payload_path.write_text(artifacts.activation_payload.model_dump_json())
        # Also record duration probed back onto the video row for next time.
        with self._conn:
            self._conn.execute(
                "UPDATE videos SET duration_s = ? WHERE id = ? AND duration_s <= 0",
                (duration_s, video.id),
            )
        # Opt-in cleanup of the source mp4s. Runs only after every
        # write above has landed — RegionMetrics in SQLite, NPZ + JSON
        # sidecar on disk — so a crash mid-cleanup never strands a
        # half-deleted video that the API still thinks is complete.
        if self.cfg.purge_after_inference:
            self._purge_video_artifacts(video.id)
        # Tier-b: drop the activation artifacts too. Independent of the
        # mp4 purge: a caller can pick either / both / neither. We do
        # this *after* the mp4 purge so log order matches the layered
        # intent ("free mp4s, then free activations"). Same crash
        # semantics — SQLite region_metrics already durable.
        if self.cfg.purge_activations:
            self._purge_activation_artifacts(artifacts, payload_path)
        return artifacts

    def _purge_activation_artifacts(
        self, artifacts: RunArtifacts, payload_path: Path,
    ) -> None:
        """Delete the .npz + .meta.json + payload .json for a completed run.

        Triggered by ``purge_activations=True``. Region metrics rows in
        SQLite stay — that's the whole point of the tier: keep the
        irreducible product, drop the per-vertex blobs that take >99%
        of the disk. Filesystem errors are logged and swallowed, same
        contract as ``_purge_video_artifacts``.

        We also clear ``inference_runs.activation_path`` so the api's
        SqliteStore-backed ``/videos/{id}/activation`` can detect that
        the per-vertex payload is gone and fall through to brain-viz's
        graceful fallback (region_metrics-only rendering).
        """
        _log.info(
            "event=cleanup_started kind=activations video_id=%s run_id=%s",
            artifacts.inference_run.video_id, artifacts.inference_run.id,
        )
        for path in (
            artifacts.activation_path,
            artifacts.sidecar_path,
            payload_path,
        ):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                _log.warning(
                    "activation purge failed for %s: %s",
                    artifacts.inference_run.video_id, exc,
                )
        with self._conn:
            self._conn.execute(
                "UPDATE inference_runs SET activation_path = '' WHERE id = ?",
                (artifacts.inference_run.id,),
            )

    def _purge_video_artifacts(self, video_id: str) -> None:
        """Delete the raw + preprocessed mp4s for a completed video.

        Best-effort: missing files are fine (mock mode never writes
        them; skip-* paths leave them off disk too). Filesystem errors
        are logged and swallowed — the inference result is already
        durable; failing the run because cleanup hit EACCES would be
        the wrong shape.
        """
        _log.info("event=cleanup_started kind=videos video_id=%s", video_id)
        for path in (
            self.cfg.videos_dir / f"{video_id}.mp4",
            self.cfg.processed_dir / f"{video_id}.mp4",
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("purge failed for %s: %s", video_id, exc)
        # Also drop the videos.local_path pointer so the catalog
        # doesn't keep advertising a file that no longer exists.
        with self._conn:
            self._conn.execute(
                "UPDATE videos SET downloaded = 0, local_path = NULL WHERE id = ?",
                (video_id,),
            )

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
        metric_rows = [
            (m.inference_run_id, m.region_id, m.video_id,
             m.mean, m.peak, m.sustained, json.dumps(m.timeseries))
            for m in artifacts.region_metrics
        ]
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
            if metric_rows:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO region_metrics
                        (inference_run_id, region_id, video_id,
                         mean, peak, sustained, timeseries_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    metric_rows,
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
