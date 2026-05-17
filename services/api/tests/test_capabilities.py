"""GET /api/v1/capabilities — frontend mode gating.

The endpoint reports whether ``real`` mode will actually work on this
host and, when it won't, which specific dependencies are missing. The
``mock`` slot is always true because MockBackend ships with the base
install and needs no external binaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from neural_media_api.import_jobs import ImportRunner
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import init_db


def _stub_factory():
    """Capabilities is read-only — no orchestrator should ever be built."""
    def factory(**_kwargs):
        raise AssertionError("capabilities endpoint must not build an orchestrator")
    return factory


def _make_client(tmp_path: Path) -> TestClient:
    db = tmp_path / "catalog.db"
    init_db(db)
    runner = ImportRunner(
        db_path=db,
        activations_dir=tmp_path / "act",
        videos_dir=tmp_path / "vid",
        processed_dir=tmp_path / "proc",
        orchestrator_factory=_stub_factory(),
    )
    app = create_app(import_runner=runner)
    return TestClient(app, base_url="http://localhost")


def test_capabilities_shape_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the probe so this assertion isn't host-dependent.
    monkeypatch.setattr(
        "neural_media_api.main.real_mode_capabilities",
        lambda: (False, ["missing-extra", "missing-ffmpeg"]),
    )
    client = _make_client(tmp_path)
    body = client.get("/api/v1/capabilities").json()
    assert set(body) == {"mock", "real", "real_blockers"}
    assert body["mock"] is True
    assert body["real"] is False
    assert body["real_blockers"] == ["missing-extra", "missing-ffmpeg"]


def test_capabilities_real_true_when_no_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "neural_media_api.main.real_mode_capabilities",
        lambda: (True, []),
    )
    client = _make_client(tmp_path)
    body = client.get("/api/v1/capabilities").json()
    assert body["real"] is True
    assert body["real_blockers"] == []


def test_capabilities_real_false_with_each_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every blocker token the contract documents must round-trip cleanly."""
    expected_tokens = {
        "missing-extra", "missing-gpu", "missing-ffmpeg", "missing-yt-dlp",
    }
    for token in expected_tokens:
        monkeypatch.setattr(
            "neural_media_api.main.real_mode_capabilities",
            lambda t=token: (False, [t]),
        )
        client = _make_client(tmp_path)
        body = client.get("/api/v1/capabilities").json()
        assert body["real"] is False, token
        assert body["real_blockers"] == [token], token


# --- real_mode_capabilities() unit checks ----------------------------------


def test_real_mode_capabilities_reports_missing_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ffmpeg / yt-dlp are missing, both tokens land in blockers."""
    monkeypatch.setattr(
        "neural_media_api.import_jobs.shutil.which",
        lambda binary: None,
    )
    # Force torch import path to fail so we also see missing-extra.
    import sys
    monkeypatch.setitem(sys.modules, "torch", None)
    from neural_media_api.import_jobs import real_mode_capabilities
    ok, blockers = real_mode_capabilities()
    assert ok is False
    assert "missing-ffmpeg" in blockers
    assert "missing-yt-dlp" in blockers
    assert "missing-extra" in blockers
