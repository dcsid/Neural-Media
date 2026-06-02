"""POST /api/v1/import/{job_id}/retry tests.

The stub orchestrator pattern from ``test_import.py`` is reused: each
stub records whether ``ingest_export`` or ``run_pending`` was called so
we can assert the retry path bypasses parsing.
"""

from __future__ import annotations

import io
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from neural_media_api.import_jobs import (
    ImportJob,
    ImportJobsStore,
    ImportRunner,
    JobAlreadyRunning,
)
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import init_db


@dataclass
class _StubSummary:
    parsed_videos: int = 2
    parsed_events: int = 5
    queued: int = 2
    completed: int = 2
    skipped_complete: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)


class _StubOrchestrator:
    """Records which entry point fired so retry tests can prove it."""

    def __init__(
        self,
        *,
        summary: _StubSummary | None = None,
        release: threading.Event | None = None,
        ingest_log: list[Path] | None = None,
        retry_log: list[bool] | None = None,
    ) -> None:
        self._summary = summary or _StubSummary()
        self._release = release
        self._ingest_log = ingest_log
        self._retry_log = retry_log
        self.closed = False

    def ingest_export(self, path: Path, **_kwargs: Any) -> _StubSummary:
        if self._ingest_log is not None:
            self._ingest_log.append(path)
        if self._release is not None:
            assert self._release.wait(timeout=10.0), "release event never set"
        return self._summary

    def run_pending(self, **_kwargs: Any) -> _StubSummary:
        if self._retry_log is not None:
            self._retry_log.append(True)
        if self._release is not None:
            assert self._release.wait(timeout=10.0), "release event never set"
        return self._summary

    def close(self) -> None:
        self.closed = True


def _factory(
    *,
    summary: _StubSummary | None = None,
    release: threading.Event | None = None,
    ingest_log: list[Path] | None = None,
    retry_log: list[bool] | None = None,
):
    def f(**_kwargs):
        return _StubOrchestrator(
            summary=summary,
            release=release,
            ingest_log=ingest_log,
            retry_log=retry_log,
        )
    return f


@pytest.fixture
def import_db(tmp_path: Path) -> Path:
    db = tmp_path / "catalog.db"
    init_db(db)
    return db


@pytest.fixture
def imports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "imports"
    monkeypatch.setenv("NEURAL_MEDIA_IMPORTS_DIR", str(d))
    return d


@pytest.fixture
def small_export_bytes() -> bytes:
    return (
        b'{"Activity":{"Video Browsing History":{"VideoList":['
        b'{"Date":"2026-05-12 08:14:03",'
        b'"Link":"https://www.tiktok.com/@u/video/1"}'
        b"]}}}"
    )


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
    app = create_app(import_runner=runner)
    return TestClient(app, base_url="http://localhost")


def _wait_status(client: TestClient, job_id: str, *, status: str, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/import/{job_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} never reached status={status} (last={body!r})")


# --- happy path: partial → retry runs run_pending --------------------------


def test_retry_on_partial_calls_run_pending(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    """A partial job's retry must call run_pending (not ingest_export)."""
    ingest_log: list[Path] = []
    retry_log: list[bool] = []

    # First POST → partial.
    client = _client(
        import_db,
        _factory(
            summary=_StubSummary(parsed_videos=2, completed=1, failed=1),
            ingest_log=ingest_log,
            retry_log=retry_log,
        ),
        tmp_path,
    )
    resp = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert resp.status_code == 200
    first_id = resp.json()["id"]
    _wait_status(client, first_id, status="partial")
    assert ingest_log, "first job should have called ingest_export"
    assert retry_log == [], "first job should not have called run_pending"

    # Now retry. The new factory yields a fresh orchestrator that will
    # only report success this time so the retry lands `complete`.
    client = _client(
        import_db,
        _factory(
            summary=_StubSummary(parsed_videos=2, completed=2, failed=0),
            ingest_log=ingest_log,
            retry_log=retry_log,
        ),
        tmp_path,
    )
    retry_resp = client.post(f"/api/v1/import/{first_id}/retry")
    assert retry_resp.status_code == 200, retry_resp.text

    new_job = retry_resp.json()
    assert new_job["id"] != first_id
    # mode + source_filename carry over from the previous job.
    assert new_job["mode"] == "mock"
    assert new_job["source_filename"] == "a.json"

    _wait_status(client, new_job["id"], status="complete")
    # Critical: retry path took the run_pending branch, not ingest_export.
    assert retry_log == [True]


def test_retry_on_failed_is_allowed(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    """``failed`` (zero completions) must also be retryable."""
    import sqlite3
    conn = sqlite3.connect(import_db)
    try:
        conn.execute(
            "INSERT INTO import_jobs "
            "(id, status, mode, created_at, updated_at, completed_at, "
            " progress_current, progress_total, progress_phase, "
            " error, source_filename) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "prev-failed", "failed", "mock",
                "2026-05-16T00:00:00+00:00",
                "2026-05-16T00:00:01+00:00",
                "2026-05-16T00:00:01+00:00",
                0, 2, None,
                "boom", "watch_history.txt",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    retry_log: list[bool] = []
    client = _client(
        import_db,
        _factory(
            summary=_StubSummary(parsed_videos=2, completed=2, failed=0),
            retry_log=retry_log,
        ),
        tmp_path,
    )
    resp = client.post("/api/v1/import/prev-failed/retry")
    assert resp.status_code == 200
    _wait_status(client, resp.json()["id"], status="complete")
    assert retry_log == [True]


# --- refusal paths ----------------------------------------------------------


def test_retry_on_unknown_job_returns_404(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    client = _client(import_db, _factory(), tmp_path)
    resp = client.post("/api/v1/import/does-not-exist/retry")
    assert resp.status_code == 404


def test_retry_on_complete_job_returns_409(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    """``complete`` jobs have nothing to retry — refuse with 409 + error_code.

    ``queued`` / ``running`` are theoretically also non-retryable, but
    ``ImportRunner.__init__`` runs ``mark_orphans_failed`` which sweeps
    any non-terminal rows to ``failed`` before any handler can see them.
    So the only "non-retryable terminal" state reachable through the API
    in practice is ``complete``.
    """
    import sqlite3
    conn = sqlite3.connect(import_db)
    try:
        conn.execute(
            "INSERT INTO import_jobs "
            "(id, status, mode, created_at, updated_at, completed_at, "
            " progress_current, progress_total, progress_phase) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "prev-complete", "complete", "mock",
                "2026-05-16T00:00:00+00:00",
                "2026-05-16T00:00:01+00:00",
                "2026-05-16T00:00:01+00:00",
                2, 2, None,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    client = _client(import_db, _factory(), tmp_path)
    resp = client.post("/api/v1/import/prev-complete/retry")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "job_not_retryable"
    assert "complete" in body["detail"]


def test_retry_while_another_job_in_flight_returns_409_with_running_job(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    """Same 409 semantics as POST /import: literal ImportJob body."""
    # Seed a partial job to retry later.
    import sqlite3
    conn = sqlite3.connect(import_db)
    try:
        conn.execute(
            "INSERT INTO import_jobs "
            "(id, status, mode, created_at, updated_at, completed_at, "
            " progress_current, progress_total, progress_phase, "
            " error, source_filename) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "prev-partial", "partial", "mock",
                "2026-05-16T00:00:00+00:00",
                "2026-05-16T00:00:01+00:00",
                "2026-05-16T00:00:01+00:00",
                1, 2, None,
                "one failed", "a.json",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    release = threading.Event()
    client = _client(import_db, _factory(release=release), tmp_path)

    # Kick off a normal import that will block on `release`.
    first = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert first.status_code == 200
    first_id = first.json()["id"]
    _wait_status(client, first_id, status="running")

    retry = client.post("/api/v1/import/prev-partial/retry")
    assert retry.status_code == 409
    body = retry.json()
    # Literal ImportJob shape — same as POST /import's 409 envelope.
    assert body["id"] == first_id
    assert body["status"] == "running"
    assert "error_code" not in body  # not a structured-error 409

    release.set()
    # Stub summary has completed=2,failed=0 → terminal "complete". The
    # exact terminal value isn't what this test cares about; we just need
    # the job to land somewhere terminal so the slot releases.
    final = None
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        final = client.get(f"/api/v1/import/{first_id}").json()
        if final["status"] in {"complete", "partial", "failed"}:
            break
        time.sleep(0.02)
    assert final is not None and final["status"] in {"complete", "partial", "failed"}


# --- structured error_code on POST /import ---------------------------------


def test_post_import_returns_error_code_on_bad_extension(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    client = _client(import_db, _factory(), tmp_path)
    resp = client.post(
        "/api/v1/import",
        files={"file": ("evil.exe", io.BytesIO(b"\x00"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "file_extension_rejected"
    assert "detail" in body


def test_post_import_returns_error_code_on_unparseable_since(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    client = _client(import_db, _factory(), tmp_path)
    resp = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
        data={"since": "tomorrow"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error_code"] == "since_unparseable"
    assert "tomorrow" in body["detail"]


def test_post_import_real_mode_blocker_codes(
    import_db: Path, imports_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, small_export_bytes: bytes,
) -> None:
    """Each capability blocker must surface as the corresponding error_code."""
    cases = [
        (["missing-extra"], "real_extra_missing"),
        (["missing-ffmpeg"], "real_no_ffmpeg"),
        (["missing-yt-dlp"], "real_no_yt_dlp"),
        (["missing-gpu"], "real_no_gpu"),
    ]
    for blockers, expected_code in cases:
        monkeypatch.setattr(
            "neural_media_api.main.real_mode_capabilities",
            lambda b=blockers: (False, b),
        )
        client = _client(import_db, _factory(), tmp_path)
        resp = client.post(
            "/api/v1/import",
            files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
            data={"mode": "real"},
        )
        assert resp.status_code == 400, (blockers, resp.text)
        body = resp.json()
        assert body["error_code"] == expected_code, (blockers, body)


# --- singleton-gate concurrency --------------------------------------------


def test_concurrent_submit_and_retry_admits_exactly_one(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    """A POST (submit) and a retry (submit_retry) racing for the slot must
    yield exactly one running job.

    Both paths funnel through ``ImportRunner._claim_slot`` under one lock,
    so no interleaving can let two orchestrators run at once. The two
    callers sync on a barrier to maximise contention; whichever wins blocks
    on ``release`` (the stub blocks both ingest_export and run_pending), so
    the loser deterministically hits the gate. We assert exactly one wins,
    the other raises ``JobAlreadyRunning`` pointing at the winner, and the
    catalog holds a single in-flight row.
    """
    import sqlite3

    # Seed a retryable 'partial' job for the retry path to target.
    conn = sqlite3.connect(import_db)
    try:
        conn.execute(
            "INSERT INTO import_jobs "
            "(id, status, mode, created_at, updated_at, completed_at, "
            " progress_current, progress_total, progress_phase, "
            " error, source_filename) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "prev-partial", "partial", "mock",
                "2026-05-16T00:00:00+00:00",
                "2026-05-16T00:00:01+00:00",
                "2026-05-16T00:00:01+00:00",
                1, 2, None,
                "one failed", "a.json",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    release = threading.Event()
    runner = ImportRunner(
        db_path=import_db,
        activations_dir=tmp_path / "act",
        videos_dir=tmp_path / "vid",
        processed_dir=tmp_path / "proc",
        orchestrator_factory=_factory(release=release),
    )

    export_path = tmp_path / "user_data.json"
    export_path.write_bytes(small_export_bytes)

    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def _submit() -> None:
        barrier.wait()
        try:
            results["submit"] = runner.submit(export_path, "mock")
        except JobAlreadyRunning as exc:
            results["submit"] = exc

    def _retry() -> None:
        barrier.wait()
        try:
            results["retry"] = runner.submit_retry("prev-partial")
        except JobAlreadyRunning as exc:
            results["retry"] = exc

    threads = [threading.Thread(target=_submit), threading.Thread(target=_retry)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert all(not t.is_alive() for t in threads), "a caller deadlocked"

    outcomes = [results["submit"], results["retry"]]
    winners = [o for o in outcomes if isinstance(o, ImportJob)]
    losers = [o for o in outcomes if isinstance(o, JobAlreadyRunning)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    # The 409 carries the job that actually holds the slot.
    assert losers[0].running_job.id == winners[0].id

    # The whole catalog holds exactly one in-flight row — proof the loser
    # never created a second job.
    conn = sqlite3.connect(import_db)
    try:
        (live,) = conn.execute(
            "SELECT COUNT(*) FROM import_jobs "
            "WHERE status NOT IN ('complete', 'partial', 'failed')"
        ).fetchone()
    finally:
        conn.close()
    assert live == 1, f"expected 1 in-flight job, found {live}"

    # Release the winner so its daemon thread + slot tear down cleanly.
    release.set()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        won = ImportJobsStore(import_db).get(winners[0].id)
        if won is not None and won.status in {"complete", "partial", "failed"}:
            break
        time.sleep(0.02)
    else:
        pytest.fail("winner never reached a terminal state after release")
