"""SQLite-backed `Store` implementation.

Schema mirrors `shared.schemas` exactly. The data-pipeline worker writes
into these tables; the API only reads. Activation payloads stay on disk
as ``.npz`` (canonical) — `get_activation` loads the array and converts
it to wire format via the helpers exposed by `neural_media_inference`.

Connection model: one short-lived `sqlite3.Connection` per call. FastAPI
runs route handlers in a threadpool, so a per-call connection sidesteps
SQLite's thread-affinity rules without `check_same_thread=False`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .schemas_re_export import (
    ActivationOutput,
    InferenceRun,
    RegionMetrics,
    VideoMetadata,
    WatchEvent,
)

# Wire-format budget — must match neural_media_inference.runner defaults so
# the SQLite-served activation looks identical to the freshly-computed one.
_WIRE_MAX_TIMEPOINTS = 120
_WIRE_NUM_KEYFRAMES = 5

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_STATEMENTS: tuple[str, ...] = (
    # Videos. tags is JSON-encoded because SQLite has no native list type.
    # first_seen_idx lets `list_videos` preserve the order the pipeline
    # discovered URLs in; the column is optional (NULL → fall back to rowid).
    # `inserted_at` is written by data-pipeline's Orchestrator (ISO-8601
    # timestamp of first ingest). Nullable here because the api doesn't
    # read it — it's pipeline-side observability. CREATE TABLE IF NOT
    # EXISTS means whichever of (api init_db, pipeline Orchestrator
    # construction) runs first wins, so the two schemas MUST be a
    # superset of both writers' needs. See note in docs/architecture.md.
    """
    CREATE TABLE IF NOT EXISTS videos (
        id              TEXT PRIMARY KEY,
        source_url      TEXT NOT NULL,
        title           TEXT,
        author          TEXT,
        duration_s      REAL NOT NULL DEFAULT 0.0,
        downloaded      INTEGER NOT NULL DEFAULT 0,
        local_path      TEXT,
        tags_json       TEXT NOT NULL DEFAULT '[]',
        first_seen_idx  INTEGER,
        inserted_at     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_events (
        id                  TEXT PRIMARY KEY,
        video_id            TEXT NOT NULL,
        watched_at          TEXT NOT NULL,
        duration_watched_s  REAL,
        completion_pct      REAL,
        source              TEXT NOT NULL DEFAULT 'tiktok_export'
    )
    """,
    # Aggregator hits watched_at on every request.
    "CREATE INDEX IF NOT EXISTS idx_watch_events_watched_at ON watch_events(watched_at)",
    "CREATE INDEX IF NOT EXISTS idx_watch_events_video_id ON watch_events(video_id)",
    """
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
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inference_runs_video_id ON inference_runs(video_id)",
    "CREATE INDEX IF NOT EXISTS idx_inference_runs_created_at ON inference_runs(created_at)",
    # Denormalized video_id on region_metrics — the aggregator scans rows by
    # video, and joining through inference_runs on every aggregate-hit is
    # wasteful. The brief explicitly asks for an index on this column.
    """
    CREATE TABLE IF NOT EXISTS region_metrics (
        inference_run_id  TEXT NOT NULL,
        region_id         TEXT NOT NULL,
        video_id          TEXT NOT NULL,
        mean              REAL NOT NULL,
        peak              REAL NOT NULL,
        sustained         REAL NOT NULL,
        timeseries_json   TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (inference_run_id, region_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_region_metrics_video_id ON region_metrics(video_id)",
    # Import-job tracking — canonical shape per shared/CONTRACTS.md §8.
    # status enum: queued | running | complete | partial | failed.
    # progress is denormalised into three columns (current/total/phase)
    # rather than a JSON blob so the status index stays cheap and the
    # poller can write counters without parsing.
    """
    CREATE TABLE IF NOT EXISTS import_jobs (
        id                 TEXT PRIMARY KEY,
        status             TEXT NOT NULL,
        mode               TEXT NOT NULL,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        completed_at       TEXT,
        progress_current   INTEGER NOT NULL DEFAULT 0,
        progress_total     INTEGER,
        progress_phase     TEXT,
        error              TEXT,
        source_filename    TEXT
    )
    """,
    # Lets the "is anything still running?" check land in one indexed lookup.
    "CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status)",
)


def init_db(db_path: Path | str) -> None:
    """Create every table + index if they don't already exist.

    Idempotent — safe to run on a populated DB. Creates parent directories
    if missing.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Row → schema decoders
# ---------------------------------------------------------------------------

def _decode_video(row: sqlite3.Row) -> VideoMetadata:
    return VideoMetadata(
        id=row["id"],
        source_url=row["source_url"],
        title=row["title"],
        author=row["author"],
        duration_s=row["duration_s"],
        downloaded=bool(row["downloaded"]),
        local_path=row["local_path"],
        tags=json.loads(row["tags_json"] or "[]"),
    )


def _decode_watch_event(row: sqlite3.Row) -> WatchEvent:
    return WatchEvent(
        id=row["id"],
        video_id=row["video_id"],
        watched_at=row["watched_at"],
        duration_watched_s=row["duration_watched_s"],
        completion_pct=row["completion_pct"],
        source=row["source"],
    )


def _decode_run(row: sqlite3.Row) -> InferenceRun:
    return InferenceRun(
        id=row["id"],
        video_id=row["video_id"],
        model_id=row["model_id"],
        model_version=row["model_version"],
        seed=row["seed"],
        params_json=json.loads(row["params_json"] or "{}"),
        created_at=row["created_at"],
        activation_path=row["activation_path"],
        status=row["status"],
    )


def _decode_metric(row: sqlite3.Row) -> RegionMetrics:
    return RegionMetrics(
        region_id=row["region_id"],
        video_id=row["video_id"],
        inference_run_id=row["inference_run_id"],
        mean=row["mean"],
        peak=row["peak"],
        sustained=row["sustained"],
        timeseries=json.loads(row["timeseries_json"] or "[]"),
    )


# ---------------------------------------------------------------------------
# SqliteStore
# ---------------------------------------------------------------------------

class SqliteStore:
    """Read-only view over `data/sqlite/neural_media.db`.

    The pipeline worker is the writer; this class never issues INSERT or
    UPDATE. Construction does not require the DB to exist — calls will
    just return empty results until `init_db()` runs.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    # --- connection plumbing ---------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # `uri=True` would let us open read-only via file:...?mode=ro, but
        # that fails if the file does not exist yet. A normal connect on a
        # missing path *creates* an empty file, so we guard explicitly.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        if not self.db_path.is_file():
            return []
        conn = self._connect()
        try:
            return list(conn.execute(sql, tuple(params)))
        except sqlite3.OperationalError:
            # Tables haven't been created yet — same observable behavior
            # as an empty DB. Avoid 500ing the API in that window.
            return []
        finally:
            conn.close()

    # --- Store protocol ---------------------------------------------------

    def list_videos(self) -> list[VideoMetadata]:
        rows = self._query(
            "SELECT * FROM videos "
            "ORDER BY COALESCE(first_seen_idx, rowid)"
        )
        return [_decode_video(r) for r in rows]

    def get_video(self, video_id: str) -> VideoMetadata | None:
        rows = self._query("SELECT * FROM videos WHERE id = ?", (video_id,))
        return _decode_video(rows[0]) if rows else None

    def get_metrics(self, video_id: str) -> list[RegionMetrics]:
        # Most-recent complete run wins. If no run is `complete`, fall back
        # to the most-recent run of any status so the user still sees what
        # was computed.
        rows = self._query(
            """
            SELECT rm.*
              FROM region_metrics rm
              JOIN inference_runs ir ON ir.id = rm.inference_run_id
             WHERE rm.video_id = ?
               AND ir.id = (
                   SELECT id FROM inference_runs
                    WHERE video_id = ?
                    ORDER BY (status = 'complete') DESC, created_at DESC
                    LIMIT 1
               )
             ORDER BY rm.region_id
            """,
            (video_id, video_id),
        )
        return [_decode_metric(r) for r in rows]

    def get_activation(self, video_id: str) -> ActivationOutput | None:
        run = self._latest_run(video_id)
        if run is None:
            return None
        activation_path = Path(run.activation_path)
        if not activation_path.is_file():
            return None

        # Lazy import — numpy is in our deps, but neural_media_inference
        # is a separate worker's package. Importing here keeps the import
        # surface for catalog-only endpoints small and skips the dep
        # entirely when get_activation is never called.
        import numpy as np
        from neural_media_inference import (
            downsample_region_means,
            keyframe_vertex_snapshots,
        )

        with np.load(activation_path) as data:
            activations = data["activations"]

        sample_rate_hz = float(run.params_json.get("sample_rate_hz", 1.0))
        num_timepoints = int(activations.shape[0])
        timestamps = (
            np.arange(num_timepoints, dtype=np.float64) / sample_rate_hz
        ).tolist()
        region_means = downsample_region_means(
            activations, max_timepoints=_WIRE_MAX_TIMEPOINTS
        )
        keyframe_vertices = keyframe_vertex_snapshots(
            activations, num_keyframes=_WIRE_NUM_KEYFRAMES, timestamps=timestamps
        )

        return ActivationOutput(
            inference_run_id=run.id,
            video_id=video_id,
            num_timepoints=num_timepoints,
            sample_rate_hz=sample_rate_hz,
            timestamps=timestamps,
            keyframe_vertices=keyframe_vertices,
            region_means=region_means,
        )

    def list_watch_events(self) -> list[WatchEvent]:
        rows = self._query(
            "SELECT * FROM watch_events ORDER BY watched_at"
        )
        return [_decode_watch_event(r) for r in rows]

    def list_runs(self) -> list[InferenceRun]:
        rows = self._query(
            "SELECT * FROM inference_runs ORDER BY created_at"
        )
        return [_decode_run(r) for r in rows]

    def version(self) -> str:
        """Cheap mutation marker used by `compute_aggregate`'s memo cache.

        Combines the inference-run count and the most recent
        ``created_at`` — any new run, regardless of update vs. insert,
        moves the marker forward.
        """
        rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(MAX(created_at), '') AS last "
            "FROM inference_runs"
        )
        if not rows:
            return "0|"
        row = rows[0]
        # Include watch-events count so a fresh import without inference
        # also bumps the version.
        we = self._query("SELECT COUNT(*) AS n FROM watch_events")
        we_n = we[0]["n"] if we else 0
        return f"{row['n']}|{row['last']}|{we_n}"

    # --- helpers ---------------------------------------------------------

    def _latest_run(self, video_id: str) -> InferenceRun | None:
        rows = self._query(
            "SELECT * FROM inference_runs "
            "WHERE video_id = ? "
            "ORDER BY (status = 'complete') DESC, created_at DESC "
            "LIMIT 1",
            (video_id,),
        )
        return _decode_run(rows[0]) if rows else None


__all__ = ["SCHEMA_STATEMENTS", "SqliteStore", "init_db"]
