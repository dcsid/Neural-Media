"""create_app() backing-store selection via NEURAL_MEDIA_DB_PATH."""

from __future__ import annotations

from pathlib import Path

import pytest
from neural_media_api.main import create_app
from neural_media_api.sqlite_store import SqliteStore
from neural_media_api.store import SampleStore


def test_db_path_env_selects_sqlite_store(
    monkeypatch: pytest.MonkeyPatch, populated_sqlite_db: Path
) -> None:
    monkeypatch.setenv("NEURAL_MEDIA_DB_PATH", str(populated_sqlite_db))
    app = create_app()
    assert isinstance(app.state.store, SqliteStore)
    # Quick read-through to prove the store actually works in this slot.
    assert len(app.state.store.list_videos()) == 2


def test_missing_db_path_env_falls_back_to_sample_store(
    monkeypatch: pytest.MonkeyPatch,
    sample_root: Path,
    tiktok_export_path: Path,
) -> None:
    monkeypatch.delenv("NEURAL_MEDIA_DB_PATH", raising=False)
    monkeypatch.setenv("NEURAL_MEDIA_SAMPLE_ROOT", str(sample_root))
    monkeypatch.setenv("NEURAL_MEDIA_TIKTOK_EXPORT", str(tiktok_export_path))
    app = create_app()
    assert isinstance(app.state.store, SampleStore)
    assert len(app.state.store.list_videos()) == 2
    assert len(app.state.store.list_watch_events()) == 3


def test_explicit_store_argument_wins(
    monkeypatch: pytest.MonkeyPatch, populated_sqlite_db: Path
) -> None:
    """Caller-supplied store must override env-based selection."""
    monkeypatch.setenv("NEURAL_MEDIA_DB_PATH", str(populated_sqlite_db))
    explicit = SampleStore(populated_sqlite_db.parent / "does-not-exist")
    app = create_app(store=explicit)
    assert app.state.store is explicit
