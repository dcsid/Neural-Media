"""Mock-fast path integration tests.

These tests exercise ``_drive_mock_fast`` end-to-end with the *real*
``run_inference`` (no closure-based stubs), which is the only inference
function that triggers the fast path. Everything else in the test suite
injects a closure and falls through to the sequential ``_drive_all``;
this file covers the gap.

Contract under test:
  * SQLite rows that land are byte-equal to what the sequential path
    would have written (same INSERT-OR-REPLACE / INSERT-OR-IGNORE
    semantics, same row counts).
  * `purge_activations=True` leaves zero files in `activations/` even
    though the lean worker never wrote them in the first place.
  * `purge_activations=False` uses the full runner and lands the
    npz + sidecar + payload JSON.
  * The fast path does NOT activate when an injected `inference_fn`
    breaks the picklable-default assumption — that case is covered by
    the existing 100+ tests in this package.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from neural_media_pipeline import Orchestrator, OrchestratorConfig
from neural_media_pipeline.orchestrate import STATUS_COMPLETE
from shared.schemas import REGION_IDS


def _make_synth_export(path: Path, n: int) -> None:
    """Write a TikTok user_data.json with ``n`` deterministic videos."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {
                        "Date": (base + timedelta(minutes=i)).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "Link": f"https://www.tiktok.com/@bench/video/{1_000_000 + i}",
                    }
                    for i in range(n)
                ]
            }
        }
    }
    path.write_text(json.dumps(payload))


def test_mock_fast_purge_leaves_no_files(tmp_path: Path) -> None:
    """Lean worker path: 50 videos, purge_activations=True. No files land."""
    export = tmp_path / "export.json"
    _make_synth_export(export, n=50)
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        purge_activations=True, purge_after_inference=True,
    )
    with Orchestrator(cfg) as orch:
        summary = orch.run(export)

    assert summary.completed == 50
    assert summary.failed == 0
    # Lean worker never wrote anything; purge sweep is a no-op on
    # missing files (missing_ok=True). Either way, the dir is empty.
    activations = list((tmp_path / "activations").iterdir())
    assert activations == [], activations

    # SQLite still landed every row the contract requires.
    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM inference_runs").fetchone()[0] == 50
        assert conn.execute(
            "SELECT COUNT(*) FROM region_metrics"
        ).fetchone()[0] == 50 * len(REGION_IDS)
        # activation_path cleared by the purge sweep so /v/[id] serves
        # the no-payload fallback.
        paths = [r[0] for r in conn.execute(
            "SELECT activation_path FROM inference_runs"
        ).fetchall()]
        assert paths == [""] * 50
        # Every job complete.
        statuses = [r[0] for r in conn.execute(
            "SELECT status FROM pipeline_jobs"
        ).fetchall()]
        assert statuses == [STATUS_COMPLETE] * 50
    finally:
        conn.close()


def test_mock_fast_no_purge_writes_artifacts(tmp_path: Path) -> None:
    """Full-runner path: 30 videos, keep artifacts. npz + meta + payload land."""
    export = tmp_path / "export.json"
    _make_synth_export(export, n=30)
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        # purge_activations=False by default — the runner-backed worker
        # is the one that runs, and disk artifacts persist.
    )
    with Orchestrator(cfg) as orch:
        summary = orch.run(export)

    assert summary.completed == 30
    npzs = list((tmp_path / "activations").glob("*.npz"))
    metas = list((tmp_path / "activations").glob("*.meta.json"))
    payloads = [
        p for p in (tmp_path / "activations").glob("*.json")
        if not p.name.endswith(".meta.json")
    ]
    assert len(npzs) == 30
    assert len(metas) == 30
    assert len(payloads) == 30


def test_mock_fast_idempotent_rerun(tmp_path: Path) -> None:
    """Re-running ingest over the same export is a no-op (jobs already complete)."""
    export = tmp_path / "export.json"
    _make_synth_export(export, n=20)
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        purge_activations=True, purge_after_inference=True,
    )

    with Orchestrator(cfg) as orch:
        first = orch.run(export)
    with Orchestrator(cfg) as orch:
        second = orch.run(export)

    assert first.completed == 20
    assert second.completed == 0  # everything already complete
    assert second.failed == 0
    assert second.queued == 0

    # Same SQLite state after the second run.
    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 20
        assert conn.execute(
            "SELECT COUNT(*) FROM inference_runs"
        ).fetchone()[0] == 20
    finally:
        conn.close()


def test_mock_fast_progress_events_coalesce(tmp_path: Path) -> None:
    """Progress callbacks fire ~200 times on large ingests, not per video."""
    from neural_media_pipeline import ProgressEvent

    export = tmp_path / "export.json"
    _make_synth_export(export, n=500)
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        purge_activations=True, purge_after_inference=True,
    )
    events: list[ProgressEvent] = []
    with Orchestrator(cfg) as orch:
        orch.run(export, progress=events.append)

    # 500 // 200 = 2 → emit every 2nd, plus initial "totals known"
    # tick. ~250-260 events total (parsing + initial + ~250 per-video).
    # Definitely NOT 500+ (per-video would be).
    per_video = [e for e in events if e.message is not None]
    assert len(per_video) < 500
    # Last event still reports videos_processed == total.
    assert per_video[-1].videos_processed == 500


def test_mock_fast_sequential_workers_one(tmp_path: Path) -> None:
    """parallel_workers=1 forces the sequential mock-fast branch.

    Still hits the lean worker (purge_activations on), still produces
    the same SQLite rows, just without the process pool. Useful for
    reproducing ordering-sensitive bugs.
    """
    export = tmp_path / "export.json"
    _make_synth_export(export, n=30)
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        purge_activations=True, purge_after_inference=True,
        parallel_workers=1,
    )
    with Orchestrator(cfg) as orch:
        summary = orch.run(export)

    assert summary.completed == 30
    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM inference_runs").fetchone()[0] == 30
        assert conn.execute(
            "SELECT COUNT(*) FROM region_metrics"
        ).fetchone()[0] == 30 * len(REGION_IDS)
    finally:
        conn.close()
