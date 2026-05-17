"""End-to-end vertical-slice test: pipeline -> api -> frontend assertion shape.

Locks the import -> catalog -> metrics -> aggregate -> capabilities path
so a refactor in any one service can't silently break the cross-service
seam. Uses the stub-orchestrator pattern from ``test_import.py`` for the
job-tracking surface, but the stub also writes real rows into the
SQLite catalog the same ``SqliteStore`` the live API reads from. That
way every GET that runs after the POST goes through the production
read path.

What this does NOT exercise:
- yt-dlp / ffmpeg (mock mode skips both at the orchestrator-config layer
  in production; we skip them here at the stub layer for the same reason).
- The real ``run_inference`` / MockBackend numpy compute. Covered in
  ``services/pipeline/tests/test_orchestrate.py::test_real_run_inference_integration``.
- The Next.js frontend itself — but we assert the shapes it consumes.
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neural_media_api.import_jobs import ImportRunner
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import SqliteStore, init_db
from neural_media_pipeline import parse_export
from shared.schemas import REGION_IDS


# ---------------------------------------------------------------------------
# Stub orchestrator that writes catalog rows
# ---------------------------------------------------------------------------


@dataclass
class _StubSummary:
    parsed_videos: int = 0
    parsed_events: int = 0
    queued: int = 0
    completed: int = 0
    skipped_complete: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)


class _DbWritingOrchestrator:
    """Parses the export, inserts real catalog rows, returns IngestSummary.

    Mirrors what the real ``neural_media_pipeline.Orchestrator`` would
    persist on a successful mock-mode ingest — videos, watch_events,
    inference_runs, region_metrics — so the SqliteStore-backed GET
    endpoints have something to return. No yt-dlp / ffmpeg / numpy.

    ``fail_first_n`` lets a test force a partial / failed terminal by
    not persisting region_metrics for the first N videos (they still
    land in ``videos`` so the catalog is consistent, but appear as
    failed in the pipeline-job sense).
    """

    def __init__(
        self,
        *,
        db_path: Path,
        activations_dir: Path,
        fail_first_n: int = 0,
        release: threading.Event | None = None,
        call_log: list[str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.activations_dir = Path(activations_dir)
        self.activations_dir.mkdir(parents=True, exist_ok=True)
        self._fail_first_n = fail_first_n
        self._release = release
        self._call_log = call_log
        self.closed = False

    def ingest_export(
        self,
        path: Path,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        **_kw: Any,
    ) -> _StubSummary:
        if self._call_log is not None:
            self._call_log.append("ingest")
        videos, events = parse_export(path, since=since, until=until)
        completed, failed = self._persist(videos, events)
        if self._release is not None:
            assert self._release.wait(timeout=10.0), "release never set"
        return _StubSummary(
            parsed_videos=len(videos),
            parsed_events=len(events),
            queued=len(videos),
            completed=completed,
            failed=failed,
        )

    def run_pending(self, **_kw: Any) -> _StubSummary:
        if self._call_log is not None:
            self._call_log.append("retry")
        # On retry we re-drive any video that has no inference run yet.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT v.id, v.source_url FROM videos v "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM inference_runs ir WHERE ir.video_id = v.id"
                ")"
            ).fetchall()
        finally:
            conn.close()
        retry_videos = [(r["id"], r["source_url"]) for r in rows]
        for vid, _url in retry_videos:
            self._write_inference(vid)
        return _StubSummary(
            parsed_videos=len(retry_videos),
            queued=len(retry_videos),
            completed=len(retry_videos),
            failed=0,
        )

    def close(self) -> None:
        self.closed = True

    # --- internals -------------------------------------------------------

    def _persist(self, videos, events) -> tuple[int, int]:
        completed = 0
        failed = 0
        conn = sqlite3.connect(self.db_path)
        try:
            for idx, v in enumerate(videos):
                conn.execute(
                    "INSERT OR IGNORE INTO videos "
                    "(id, source_url, title, author, duration_s, downloaded, "
                    " local_path, tags_json, first_seen_idx) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (v.id, v.source_url, v.title, v.author, v.duration_s,
                     0, None, json.dumps(v.tags), idx),
                )
            for e in events:
                conn.execute(
                    "INSERT OR IGNORE INTO watch_events "
                    "(id, video_id, watched_at, duration_watched_s, "
                    " completion_pct, source) "
                    "VALUES (?,?,?,?,?,?)",
                    (e.id, e.video_id, e.watched_at.isoformat(),
                     e.duration_watched_s, e.completion_pct, e.source),
                )
            conn.commit()
        finally:
            conn.close()

        for i, v in enumerate(videos):
            if i < self._fail_first_n:
                failed += 1
                continue
            self._write_inference(v.id)
            completed += 1
        return completed, failed

    def _write_inference(self, video_id: str) -> None:
        run_id = f"run-{video_id[:8]}-{uuid.uuid4().hex[:6]}"
        # No real .npz needed for the metrics / aggregate path — those
        # only read the region_metrics rows. /activation would need one,
        # which we don't exercise here.
        activation_path = str(self.activations_dir / f"{run_id}.npz")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO inference_runs "
                "(id, video_id, model_id, model_version, seed, params_json, "
                " created_at, activation_path, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id, video_id, "tribe-v2-mock", "0.0.0-mock", 7,
                    json.dumps({"sample_rate_hz": 1.5}),
                    datetime.now(timezone.utc).isoformat(),
                    activation_path, "complete",
                ),
            )
            for i, region in enumerate(REGION_IDS):
                base = 0.1 + 0.05 * i
                ts = [round(base + 0.01 * t, 4) for t in range(6)]
                conn.execute(
                    "INSERT INTO region_metrics "
                    "(inference_run_id, region_id, video_id, "
                    " mean, peak, sustained, timeseries_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id, region, video_id,
                        round(sum(ts) / len(ts), 4),
                        max(ts),
                        round(min(ts), 4),
                        json.dumps(ts),
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def _factory(
    *,
    fail_first_n: int = 0,
    release: threading.Event | None = None,
    call_log: list[str] | None = None,
):
    def f(*, db_path, activations_dir, **_kw):
        return _DbWritingOrchestrator(
            db_path=db_path,
            activations_dir=activations_dir,
            fail_first_n=fail_first_n,
            release=release,
            call_log=call_log,
        )
    return f


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_db(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    init_db(db)
    return db


@pytest.fixture
def e2e_imports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "imports"
    monkeypatch.setenv("NEURAL_MEDIA_IMPORTS_DIR", str(d))
    return d


@pytest.fixture
def five_event_txt() -> bytes:
    """5 share-shortlink records all stamped in the last hour.

    All dates are well inside a 24h ``since`` window so nothing is
    filtered out before the orchestrator sees them. Format matches the
    newer ``Watch History.txt`` exports.
    """
    now = datetime.now(timezone.utc)
    lines = []
    for i in range(5):
        # 10, 20, 30, ... minutes ago — keeps them deterministically
        # in-window even if the test takes a few seconds to run.
        ts = now - timedelta(minutes=10 * (i + 1))
        lines.append(f"Date: {ts.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
        lines.append(
            f"Link: https://www.tiktokv.com/share/video/900000000000000000{i}/\n"
        )
        lines.append("\n")
    return "".join(lines).encode("utf-8")


def _client(
    db_path: Path,
    factory,
    tmp_path: Path,
) -> TestClient:
    runner = ImportRunner(
        db_path=db_path,
        activations_dir=tmp_path / "act",
        videos_dir=tmp_path / "vid",
        processed_dir=tmp_path / "proc",
        orchestrator_factory=factory,
    )
    # Bind the catalog reads to the same DB the runner writes to so the
    # e2e GETs return what the POST just persisted.
    store = SqliteStore(db_path)
    app = create_app(store=store, import_runner=runner)
    return TestClient(app, base_url="http://localhost")


def _wait_status(
    client: TestClient,
    job_id: str,
    *,
    status: str,
    timeout_s: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(f"/api/v1/import/{job_id}").json()
        if last.get("status") == status:
            return last
        time.sleep(0.02)
    pytest.fail(f"job {job_id} never reached status={status}; last={last!r}")


# ---------------------------------------------------------------------------
# Vertical slice
# ---------------------------------------------------------------------------


def test_e2e_full_vertical_slice(
    e2e_db: Path,
    e2e_imports_dir: Path,
    tmp_path: Path,
    five_event_txt: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop a .txt, poll to completion, read every catalog endpoint."""
    # Pin capabilities so the assertion is host-independent.
    monkeypatch.setattr(
        "neural_media_api.main.real_mode_capabilities",
        lambda: (False, ["missing-extra"]),
    )

    client = _client(e2e_db, _factory(), tmp_path)

    # 1. POST /api/v1/import with the 5-event .txt and a 24h window.
    resp = client.post(
        "/api/v1/import",
        files={
            "file": (
                "Watch History.txt",
                io.BytesIO(five_event_txt),
                "text/plain",
            ),
        },
        data={"days": "1"},
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["id"]

    # 2. Poll /api/v1/import/{id} until complete.
    final_job = _wait_status(client, job_id, status="complete")
    assert final_job["progress"]["current"] == 5
    assert final_job["progress"]["total"] == 5
    assert final_job["error"] is None
    assert final_job["completed_at"] is not None

    # 3. GET /api/v1/videos — exactly 5.
    videos_resp = client.get("/api/v1/videos")
    assert videos_resp.status_code == 200
    videos = videos_resp.json()
    assert len(videos) == 5
    # Each row carries the contract fields the frontend reads.
    for v in videos:
        assert {"id", "source_url", "title", "author", "duration_s",
                "downloaded", "local_path", "tags"}.issubset(v)
        assert isinstance(v["id"], str) and v["id"]

    # 4. GET /api/v1/videos/{id}/metrics — 8 regions, values in [0,1].
    video_id = videos[0]["id"]
    metrics_resp = client.get(f"/api/v1/videos/{video_id}/metrics")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert len(metrics) == 8
    assert {m["region_id"] for m in metrics} == set(REGION_IDS)
    for m in metrics:
        for k in ("mean", "peak", "sustained"):
            assert 0.0 <= m[k] <= 1.0, (m["region_id"], k, m[k])
        assert isinstance(m["timeseries"], list)
        assert m["timeseries"], "timeseries must be non-empty"
        for v in m["timeseries"]:
            assert 0.0 <= v <= 1.0

    # 5. GET /api/v1/aggregate — by_region populated, total_videos matches.
    agg_resp = client.get("/api/v1/aggregate")
    assert agg_resp.status_code == 200
    agg = agg_resp.json()
    assert agg["total_videos"] == 5
    assert set(agg["by_region"]) == set(REGION_IDS)
    for region, bucket in agg["by_region"].items():
        assert 0.0 <= bucket["mean"] <= 1.0
        assert 0.0 <= bucket["peak"] <= 1.0
    assert len(agg["by_hour_of_day"]) == 24
    assert len(agg["by_day_of_week"]) == 7
    assert agg["first_watched_at"] is not None
    assert agg["last_watched_at"] is not None

    # 6. GET /api/v1/capabilities — shape is the one the frontend keys off of.
    cap_resp = client.get("/api/v1/capabilities")
    assert cap_resp.status_code == 200
    cap = cap_resp.json()
    assert set(cap) == {"mock", "real", "real_blockers"}
    assert cap["mock"] is True
    assert cap["real"] is False
    assert cap["real_blockers"] == ["missing-extra"]


def test_e2e_second_post_while_running_returns_409_with_running_job(
    e2e_db: Path,
    e2e_imports_dir: Path,
    tmp_path: Path,
    five_event_txt: bytes,
) -> None:
    release = threading.Event()
    client = _client(e2e_db, _factory(release=release), tmp_path)

    first = client.post(
        "/api/v1/import",
        files={
            "file": (
                "Watch History.txt",
                io.BytesIO(five_event_txt),
                "text/plain",
            ),
        },
        data={"days": "1"},
    )
    assert first.status_code == 200
    first_id = first.json()["id"]
    _wait_status(client, first_id, status="running")

    second = client.post(
        "/api/v1/import",
        files={
            "file": (
                "Watch History.txt",
                io.BytesIO(five_event_txt),
                "text/plain",
            ),
        },
        data={"days": "1"},
    )
    assert second.status_code == 409
    body = second.json()
    # Per CONTRACTS.md §8: 409 body is the literal ImportJob, not an
    # error envelope. The frontend pulls .id off it to resume polling.
    assert body["id"] == first_id
    assert body["status"] in {"queued", "running"}
    assert "error_code" not in body

    release.set()
    _wait_status(client, first_id, status="complete")


def test_e2e_retry_on_partial_spawns_new_job(
    e2e_db: Path,
    e2e_imports_dir: Path,
    tmp_path: Path,
    five_event_txt: bytes,
) -> None:
    """First POST lands ``partial`` (2/5 failed); retry walks the rest."""
    call_log: list[str] = []
    client = _client(
        e2e_db,
        _factory(fail_first_n=2, call_log=call_log),
        tmp_path,
    )

    resp = client.post(
        "/api/v1/import",
        files={
            "file": (
                "Watch History.txt",
                io.BytesIO(five_event_txt),
                "text/plain",
            ),
        },
        data={"days": "1"},
    )
    assert resp.status_code == 200
    first_id = resp.json()["id"]
    partial = _wait_status(client, first_id, status="partial")
    assert partial["progress"]["current"] == 3   # completed (excludes failed)
    assert partial["progress"]["total"] == 5
    assert call_log == ["ingest"]

    # Only 3 of 5 videos have a complete inference run at this point —
    # the retry path picks up the 2 stragglers.
    retry_resp = client.post(f"/api/v1/import/{first_id}/retry")
    assert retry_resp.status_code == 200, retry_resp.text
    retry_job = retry_resp.json()
    assert retry_job["id"] != first_id
    # mode + source_filename carry over from the previous job.
    assert retry_job["mode"] == "mock"
    assert retry_job["source_filename"] == "Watch History.txt"

    _wait_status(client, retry_job["id"], status="complete")
    # Retry MUST take the run_pending branch — not re-parse the export.
    assert call_log == ["ingest", "retry"]

    # After retry: every video has at least one inference_run row.
    conn = sqlite3.connect(e2e_db)
    try:
        n_runs = conn.execute(
            "SELECT COUNT(*) FROM inference_runs"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n_runs == 5
