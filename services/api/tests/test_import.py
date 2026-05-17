"""Import flow tests.

Uses a stub orchestrator so no yt-dlp / ffmpeg / TRIBE is touched. The
stub records what it was handed, waits on a controllable Event to
simulate work, and emits IngestSummary-shaped attributes the runner
reads to update the job row on completion.
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
from neural_media_api.import_jobs import ImportRunner
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import init_db


# --- stub orchestrator -----------------------------------------------------


@dataclass
class _StubSummary:
    """Shape the runner reads off the orchestrator's return value."""
    parsed_videos: int = 2
    parsed_events: int = 5
    queued: int = 2
    completed: int = 2
    skipped_complete: int = 0
    failed: int = 0
    errors: list = field(default_factory=list)


class _StubOrchestrator:
    """Records calls; blocks ingest_export on a release event if provided."""

    def __init__(
        self,
        *,
        release: threading.Event | None = None,
        summary: _StubSummary | None = None,
        raise_with: Exception | None = None,
        seen_paths: list[Path] | None = None,
    ) -> None:
        self._release = release
        self._summary = summary or _StubSummary()
        self._raise = raise_with
        self._seen_paths = seen_paths
        self.closed = False

    def ingest_export(self, path: Path) -> _StubSummary:
        if self._seen_paths is not None:
            self._seen_paths.append(path)
        if self._release is not None:
            assert self._release.wait(timeout=10.0), "release event never set"
        if self._raise is not None:
            raise self._raise
        return self._summary

    def close(self) -> None:
        self.closed = True


def _stub_factory(
    *,
    release: threading.Event | None = None,
    summary: _StubSummary | None = None,
    raise_with: Exception | None = None,
    seen_paths: list[Path] | None = None,
):
    def factory(**_kwargs: Any) -> _StubOrchestrator:
        return _StubOrchestrator(
            release=release,
            summary=summary,
            raise_with=raise_with,
            seen_paths=seen_paths,
        )
    return factory


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def import_db(tmp_path: Path) -> Path:
    """Initialized SQLite catalog DB (includes the import_jobs table)."""
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


def _make_client(
    db_path: Path,
    factory,
    activations_dir: Path,
) -> TestClient:
    runner = ImportRunner(
        db_path=db_path,
        activations_dir=activations_dir,
        videos_dir=activations_dir / "videos",
        processed_dir=activations_dir / "processed",
        orchestrator_factory=factory,
    )
    app = create_app(import_runner=runner)
    return TestClient(app, base_url="http://localhost")


def _wait_until_status(
    client: TestClient, job_id: str, *, status: str, timeout_s: float = 5.0
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/import/{job_id}").json()
        if body["status"] == status:
            return body
        time.sleep(0.02)
    pytest.fail(f"job {job_id} never reached status={status} (last={body!r})")


# --- happy-path JSON --------------------------------------------------------


def test_post_import_json_round_trip(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    seen: list[Path] = []
    client = _make_client(
        import_db,
        _stub_factory(seen_paths=seen, summary=_StubSummary(parsed_videos=2, completed=2)),
        tmp_path / "activations",
    )

    resp = client.post(
        "/api/v1/import",
        files={"file": ("user_data.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["mode"] == "mock"
    assert job["status"] in {"queued", "parsing", "inferring", "complete"}
    assert job["completed_at"] is None or job["status"] == "complete"

    final = _wait_until_status(client, job["id"], status="complete")
    assert final["videos_total"] == 2
    assert final["videos_processed"] == 2
    assert final["error"] is None
    assert final["completed_at"] is not None

    # The orchestrator received the staged path under the imports dir.
    assert len(seen) == 1
    assert seen[0].parent == imports_dir
    assert seen[0].read_bytes() == small_export_bytes


# --- ZIP path --------------------------------------------------------------


def test_post_import_zip_is_handed_through_unmodified(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    seen: list[Path] = []
    client = _make_client(
        import_db, _stub_factory(seen_paths=seen), tmp_path / "act",
    )
    zip_bytes = b"PK\x03\x04not-a-real-zip-just-the-bytes"
    resp = client.post(
        "/api/v1/import",
        files={"file": ("tiktok_export.zip", io.BytesIO(zip_bytes), "application/zip")},
        data={"mode": "mock"},
    )
    assert resp.status_code == 202, resp.text
    _wait_until_status(client, resp.json()["id"], status="complete")

    assert seen and seen[0].suffix == ".zip"
    assert seen[0].read_bytes() == zip_bytes


# --- rejected uploads ------------------------------------------------------


def test_post_import_rejects_other_extensions(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    client = _make_client(import_db, _stub_factory(), tmp_path / "act")
    resp = client.post(
        "/api/v1/import",
        files={"file": ("evil.exe", io.BytesIO(b"\x00\x01"), "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_post_import_real_mode_400_when_extra_missing(
    import_db: Path, imports_dir: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, small_export_bytes: bytes,
) -> None:
    monkeypatch.setattr(
        "neural_media_api.main.real_mode_available", lambda: False
    )
    client = _make_client(import_db, _stub_factory(), tmp_path / "act")
    resp = client.post(
        "/api/v1/import",
        files={"file": ("user_data.json", io.BytesIO(small_export_bytes), "application/json")},
        data={"mode": "real"},
    )
    assert resp.status_code == 400
    assert "[real]" in resp.json()["detail"]


# --- 409 dedupe ------------------------------------------------------------


def test_second_post_while_running_returns_409(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    release = threading.Event()
    client = _make_client(
        import_db, _stub_factory(release=release), tmp_path / "act",
    )

    first = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert first.status_code == 202
    first_id = first.json()["id"]

    # The runner spawns a daemon thread; give it a beat to register the job.
    _wait_until_status(client, first_id, status="inferring", timeout_s=2.0)

    second = client.post(
        "/api/v1/import",
        files={"file": ("b.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["running_job_id"] == first_id

    release.set()
    _wait_until_status(client, first_id, status="complete")

    # Once the first one is done, a third POST is accepted again.
    release2 = threading.Event()
    release2.set()  # don't block this one
    client2 = _make_client(
        import_db, _stub_factory(release=release2), tmp_path / "act2",
    )
    third = client2.post(
        "/api/v1/import",
        files={"file": ("c.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert third.status_code == 202


# --- failure path ----------------------------------------------------------


def test_orchestrator_exception_marks_job_failed(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    client = _make_client(
        import_db,
        _stub_factory(raise_with=RuntimeError("boom")),
        tmp_path / "act",
    )
    resp = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert resp.status_code == 202
    final = _wait_until_status(client, resp.json()["id"], status="failed")
    assert "boom" in final["error"]
    assert final["completed_at"] is not None


# --- GET 404 ---------------------------------------------------------------


def test_get_import_unknown_id_404(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    client = _make_client(import_db, _stub_factory(), tmp_path / "act")
    resp = client.get("/api/v1/import/does-not-exist")
    assert resp.status_code == 404


# --- orphan recovery -------------------------------------------------------


def test_orphan_running_row_does_not_block_new_imports(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    """A row left in 'inferring' from a crashed prior process must be
    cleared by ImportRunner construction so the gate isn't stuck."""
    import sqlite3
    conn = sqlite3.connect(import_db)
    try:
        conn.execute(
            "INSERT INTO import_jobs "
            "(id, status, mode, videos_total, videos_processed, started_at) "
            "VALUES (?,?,?,?,?,?)",
            ("orphan", "inferring", "mock", 0, 0, "2026-05-15T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    client = _make_client(import_db, _stub_factory(), tmp_path / "act")

    # Before the new POST: orphan flipped to failed.
    body = client.get("/api/v1/import/orphan").json()
    assert body["status"] == "failed"
    assert "orphan" in body["error"]

    # New POST goes through.
    resp = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    )
    assert resp.status_code == 202


# --- ImportJob shape -------------------------------------------------------


def test_get_import_returns_full_import_job_shape(
    import_db: Path, imports_dir: Path, tmp_path: Path, small_export_bytes: bytes,
) -> None:
    """Locks down the wire fields the frontend will consume."""
    client = _make_client(
        import_db,
        _stub_factory(summary=_StubSummary(parsed_videos=3, completed=3)),
        tmp_path / "act",
    )
    job_id = client.post(
        "/api/v1/import",
        files={"file": ("a.json", io.BytesIO(small_export_bytes), "application/json")},
    ).json()["id"]
    _wait_until_status(client, job_id, status="complete")
    body = client.get(f"/api/v1/import/{job_id}").json()
    assert set(body) == {
        "id", "status", "mode", "videos_total", "videos_processed",
        "started_at", "completed_at", "error", "message",
    }
    assert body["videos_total"] == 3
    assert body["videos_processed"] == 3


# --- CORS exposes POST -----------------------------------------------------


def test_cors_preflight_allows_post_for_import(
    import_db: Path, imports_dir: Path, tmp_path: Path,
) -> None:
    client = _make_client(import_db, _stub_factory(), tmp_path / "act")
    resp = client.options(
        "/api/v1/import",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    allowed = resp.headers.get("access-control-allow-methods", "")
    assert "POST" in allowed


