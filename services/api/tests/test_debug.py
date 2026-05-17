"""GET /api/v1/debug — host-side observability snapshot.

Exercises the read-only payload the local UI's diagnostics panel pulls
to render version / counts / disk usage / latest import. All assertions
target observable wire shape — no internal-state probing.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from neural_media_api.import_jobs import ImportRunner
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import init_db


def _stub_factory():
    """Debug is read-only — no orchestrator should ever be built."""
    def factory(**_kwargs):
        raise AssertionError("debug endpoint must not build an orchestrator")
    return factory


def _make_client(tmp_path: Path) -> tuple[TestClient, Path, Path, Path, Path]:
    """Build a TestClient + the four paths the runner is bound to.

    Returns (client, db_path, videos_dir, activations_dir, imports_dir)
    so tests can seed files / rows directly without re-deriving paths.
    """
    db = tmp_path / "catalog.db"
    init_db(db)
    videos_dir = tmp_path / "vid"
    activations_dir = tmp_path / "act"
    imports_dir = tmp_path / "imports"
    runner = ImportRunner(
        db_path=db,
        activations_dir=activations_dir,
        videos_dir=videos_dir,
        processed_dir=tmp_path / "proc",
        orchestrator_factory=_stub_factory(),
    )
    # Point the route's _imports_dir() at our tmp path. The fixture
    # would need monkeypatch to do this; the test passes its own
    # monkeypatch in via the caller.
    app = create_app(import_runner=runner)
    client = TestClient(app, base_url="http://localhost")
    return client, db, videos_dir, activations_dir, imports_dir


def _pin_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ok: bool = True,
    blockers: list[str] | None = None,
) -> None:
    """Pin real_mode_capabilities so the test isn't host-dependent.

    The /debug route imports build_debug_payload at call time, which in
    turn imports real_mode_capabilities from .import_jobs — so we
    monkeypatch it on the debug module (which is where the symbol is
    looked up at call time).
    """
    monkeypatch.setattr(
        "neural_media_api.debug.real_mode_capabilities",
        lambda: (ok, blockers or []),
    )


def _pin_imports_dir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Point the route's resolved imports dir at the test's tmp path."""
    monkeypatch.setenv("NEURAL_MEDIA_IMPORTS_DIR", str(path))


def test_debug_shape_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch, ok=True, blockers=[])
    client, _, _, _, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    body = client.get("/api/v1/debug").json()
    assert set(body) == {
        "version", "db_path", "videos_dir",
        "counts", "latest_import", "capabilities",
        "disk_usage", "uptime_s",
    }
    assert body["counts"] == {
        "videos": 0, "watch_events": 0, "inference_runs": 0, "import_jobs": 0,
    }
    assert body["latest_import"] is None
    assert body["capabilities"] == {"mock": True, "real": True, "real_blockers": []}
    assert body["disk_usage"] == {
        "videos": 0, "activations": 0, "imports": 0, "sqlite": body["disk_usage"]["sqlite"],
    }
    # init_db creates the file, so sqlite > 0 even on a fresh DB.
    assert body["disk_usage"]["sqlite"] > 0
    assert isinstance(body["uptime_s"], float)
    assert body["uptime_s"] >= 0


def test_debug_counts_reflect_db_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch)
    client, db, _, _, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    conn = sqlite3.connect(db)
    try:
        # 2 videos
        for vid in ("v1", "v2"):
            conn.execute(
                "INSERT INTO videos (id, source_url) VALUES (?, ?)",
                (vid, f"https://example.com/{vid}"),
            )
        # 3 watch_events
        for i, vid in enumerate(("v1", "v1", "v2")):
            conn.execute(
                "INSERT INTO watch_events (id, video_id, watched_at) "
                "VALUES (?, ?, ?)",
                (f"w{i}", vid, "2026-05-12T08:14:03+00:00"),
            )
        # 1 inference_run
        conn.execute(
            "INSERT INTO inference_runs "
            "(id, video_id, model_id, model_version, seed, "
            " created_at, activation_path, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("r1", "v1", "mock", "0.0.1", 0,
             "2026-05-12T08:14:03+00:00", "/tmp/a.npz", "complete"),
        )
        conn.commit()
    finally:
        conn.close()

    body = client.get("/api/v1/debug").json()
    assert body["counts"] == {
        "videos": 2, "watch_events": 3, "inference_runs": 1, "import_jobs": 0,
    }


def test_debug_latest_import_returns_most_recent_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch)
    client, db, _, _, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    conn = sqlite3.connect(db)
    try:
        rows = [
            ("older", "complete", "mock",
             "2026-05-10T00:00:00+00:00", "2026-05-10T00:00:00+00:00"),
            ("newer", "failed", "mock",
             "2026-05-15T00:00:00+00:00", "2026-05-15T00:00:00+00:00"),
        ]
        for rid, status, mode, created, updated in rows:
            conn.execute(
                "INSERT INTO import_jobs "
                "(id, status, mode, created_at, updated_at, "
                " progress_current, progress_total, progress_phase) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, status, mode, created, updated, 0, None, None),
            )
        conn.commit()
    finally:
        conn.close()

    body = client.get("/api/v1/debug").json()
    assert body["latest_import"] is not None
    assert body["latest_import"]["id"] == "newer"
    # Confirm we got the full ImportJob shape (model_dump output).
    assert set(body["latest_import"]) == {
        "id", "status", "mode",
        "created_at", "updated_at", "completed_at",
        "progress", "error", "source_filename",
    }


def test_debug_disk_usage_sums_file_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch)
    client, _, videos_dir, activations_dir, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    videos_dir.mkdir(parents=True, exist_ok=True)
    activations_dir.mkdir(parents=True, exist_ok=True)
    imports_dir.mkdir(parents=True, exist_ok=True)

    (videos_dir / "a.mp4").write_bytes(b"x" * 100)
    # Nested file in a subdir — rglob must catch it.
    (videos_dir / "sub").mkdir()
    (videos_dir / "sub" / "b.mp4").write_bytes(b"x" * 50)

    (activations_dir / "r1.npz").write_bytes(b"y" * 200)
    (imports_dir / "raw.json").write_bytes(b"z" * 25)

    body = client.get("/api/v1/debug").json()
    assert body["disk_usage"]["videos"] == 150
    assert body["disk_usage"]["activations"] == 200
    assert body["disk_usage"]["imports"] == 25
    assert body["disk_usage"]["sqlite"] > 0


def test_debug_uptime_increases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch)
    client, _, _, _, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    first = client.get("/api/v1/debug").json()["uptime_s"]
    time.sleep(0.05)
    second = client.get("/api/v1/debug").json()["uptime_s"]
    assert second > first


def test_debug_capabilities_threaded_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_capabilities(monkeypatch, ok=False, blockers=["missing-gpu"])
    client, _, _, _, imports_dir = _make_client(tmp_path)
    _pin_imports_dir(monkeypatch, imports_dir)

    body = client.get("/api/v1/debug").json()
    assert body["capabilities"] == {
        "mock": True, "real": False, "real_blockers": ["missing-gpu"],
    }
