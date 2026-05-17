"""SqliteStore: schema init + reads against a seeded catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from neural_media_api.sqlite_store import SCHEMA_STATEMENTS, SqliteStore, init_db


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    init_db(db)
    init_db(db)  # second call must not raise
    assert db.is_file()


def test_init_db_creates_every_expected_table(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    init_db(db)
    conn = sqlite3.connect(db)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        conn.close()
    assert {"videos", "watch_events", "inference_runs", "region_metrics"} <= names


def test_required_indexes_exist(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    init_db(db)
    conn = sqlite3.connect(db)
    try:
        index_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    finally:
        conn.close()
    # Brief requires these two specifically; the others are bonus.
    assert "idx_watch_events_watched_at" in index_names
    assert "idx_region_metrics_video_id" in index_names


def test_sqlite_store_against_missing_file_returns_empty(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "does-not-exist.db")
    assert store.list_videos() == []
    assert store.list_watch_events() == []
    assert store.list_runs() == []
    assert store.get_video("any") is None
    assert store.get_metrics("any") == []
    assert store.get_activation("any") is None


def test_sqlite_store_against_uninitialized_db_returns_empty(tmp_path: Path) -> None:
    """A file exists, but no tables — store treats it as empty, no 500."""
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    store = SqliteStore(db)
    assert store.list_videos() == []
    assert store.list_runs() == []


def test_sqlite_store_reads_seeded_catalog(
    populated_sqlite_db: Path,
    populated_video_ids: list[str],
    populated_regions: tuple[str, ...],
) -> None:
    store = SqliteStore(populated_sqlite_db)

    videos = store.list_videos()
    # `first_seen_idx` should preserve insertion order.
    assert [v.id for v in videos] == populated_video_ids

    metrics = store.get_metrics(populated_video_ids[0])
    assert {m.region_id for m in metrics} == set(populated_regions)

    activation = store.get_activation(populated_video_ids[0])
    assert activation is not None
    # Wire-format conversion went through neural_media_inference helpers
    # against the seeded NPZ.
    assert set(activation.region_means) == set(populated_regions)
    assert activation.num_timepoints == 6


def test_sqlite_store_get_metrics_prefers_complete_run(
    populated_sqlite_db: Path, populated_video_ids: list[str]
) -> None:
    """If a newer pending run exists, get_metrics still returns the complete run's rows."""
    vid = populated_video_ids[0]
    conn = sqlite3.connect(populated_sqlite_db)
    try:
        conn.execute(
            "INSERT INTO inference_runs "
            "(id, video_id, model_id, model_version, seed, params_json, "
            " created_at, activation_path, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "run-pending",
                vid,
                "tribe-v2-mock",
                "0.0.0-mock",
                7,
                "{}",
                "2030-01-01T00:00:00+00:00",
                "/nonexistent.npz",
                "pending",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    store = SqliteStore(populated_sqlite_db)
    metrics = store.get_metrics(vid)
    # Metrics still come from the original complete run, not the pending one.
    assert metrics, "expected metrics from the complete run"
    assert all(m.inference_run_id != "run-pending" for m in metrics)


def test_version_changes_when_new_run_lands(populated_sqlite_db: Path) -> None:
    store = SqliteStore(populated_sqlite_db)
    v_before = store.version()

    conn = sqlite3.connect(populated_sqlite_db)
    try:
        conn.execute(
            "INSERT INTO inference_runs "
            "(id, video_id, model_id, model_version, seed, params_json, "
            " created_at, activation_path, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "run-extra",
                "vid-extra",
                "tribe-v2-mock",
                "0.0.0-mock",
                7,
                "{}",
                "2099-01-01T00:00:00+00:00",
                "/nonexistent.npz",
                "complete",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert store.version() != v_before


def test_schema_statements_are_all_idempotent_create_statements() -> None:
    """Guardrail: every statement must use IF NOT EXISTS so init_db is safe to rerun."""
    for stmt in SCHEMA_STATEMENTS:
        normalized = " ".join(stmt.split()).upper()
        assert "IF NOT EXISTS" in normalized, stmt
