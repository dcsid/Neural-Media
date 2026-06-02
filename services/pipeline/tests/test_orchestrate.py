"""Orchestrator integration tests.

End-to-end exercise of the queue: download → preprocess → infer, all
stubbed. No yt-dlp, no ffmpeg, no ffprobe, no GPU. The fixture-driven
test ingests the committed 8-video TikTok sample and asserts the on-disk
SQLite state matches the contract api-orchestrator's SqliteStore will
later read.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from neural_media_inference import RunArtifacts
from neural_media_inference.runner import DEFAULT_PREPROCESSING_PARAMS

from neural_media_pipeline.orchestrate import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    Orchestrator,
    OrchestratorConfig,
    _seed_for,
)
from shared.schemas import (
    ActivationOutput,
    InferenceRun,
    NUM_VERTICES,
    REGION_IDS,
    RegionMetrics,
    VideoMetadata,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"


# ---------------------------------------------------------------------------
# Fake side effects
# ---------------------------------------------------------------------------

def _fake_fetch_writes_stub(url: str, dest: Path, ua: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"FAKE_DOWNLOAD")


def _fake_ffmpeg_writes_stub(args: list[str]) -> None:
    out = Path(args[-1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"FAKE_PREPROCESSED")


def _fake_probe(_: Path) -> float:
    return 20.0


def _fake_inference_factory():
    """Return an inference stub that yields a minimal valid RunArtifacts.

    Generates one InferenceRun + eight RegionMetrics + one
    ActivationOutput per call. Captures invocations so tests can assert
    the call shape.
    """
    calls: list[dict] = []

    def fake(*, video_id, duration_s, seed, sample_rate_hz, activations_dir,
             extra_params=None, **kw):
        calls.append({
            "video_id": video_id, "duration_s": duration_s, "seed": seed,
            "sample_rate_hz": sample_rate_hz,
            "extra_params": dict(extra_params or {}),
        })
        run_id = f"run-{video_id}"
        activations_dir = Path(activations_dir)
        activations_dir.mkdir(parents=True, exist_ok=True)
        activation_path = activations_dir / f"{run_id}.npz"
        activation_path.write_bytes(b"FAKE_NPZ")
        sidecar_path = activations_dir / f"{run_id}.meta.json"
        sidecar_path.write_text("{}")

        n_t = max(8, int(duration_s * sample_rate_hz))
        ts = [i / sample_rate_hz for i in range(n_t)]
        run = InferenceRun(
            id=run_id, video_id=video_id, model_id="tribe-v2-mock",
            model_version="0.0.0-mock", seed=seed,
            params_json={"duration_s": duration_s, "preprocessing": extra_params or {}},
            created_at=datetime.now(timezone.utc),
            activation_path=str(activation_path), status="complete",
        )
        metrics = [
            RegionMetrics(
                region_id=region, video_id=video_id, inference_run_id=run_id,
                mean=0.1, peak=0.5, sustained=0.05, timeseries=[0.1] * n_t,
            )
            for region in REGION_IDS
        ]
        payload = ActivationOutput(
            inference_run_id=run_id, video_id=video_id,
            num_vertices=NUM_VERTICES, num_timepoints=n_t,
            sample_rate_hz=sample_rate_hz, timestamps=ts,
            keyframe_vertices={"0.0": [0.0] * 4}, region_means={r: [0.1] * n_t for r in REGION_IDS},
        )
        return RunArtifacts(
            inference_run=run, region_metrics=metrics,
            activation_payload=payload, activation_path=activation_path,
            sidecar_path=sidecar_path,
        )

    return fake, calls


# ---------------------------------------------------------------------------
# Config factory
# ---------------------------------------------------------------------------

def _make_orch(
    tmp_path: Path,
    *,
    purge_after_inference: bool = False,
    purge_activations: bool = False,
    **stubs,
) -> tuple[Orchestrator, list[dict]]:
    cfg = OrchestratorConfig(
        db_path=tmp_path / "sqlite" / "neural_media.db",
        videos_dir=tmp_path / "videos",
        processed_dir=tmp_path / "videos_processed",
        activations_dir=tmp_path / "activations",
        purge_after_inference=purge_after_inference,
        purge_activations=purge_activations,
    )
    fake_inf, calls = _fake_inference_factory()
    orch = Orchestrator(
        cfg,
        fetch=stubs.get("fetch", _fake_fetch_writes_stub),
        ffmpeg=stubs.get("ffmpeg", _fake_ffmpeg_writes_stub),
        sleep=stubs.get("sleep", lambda _s: None),
        probe_duration=stubs.get("probe", _fake_probe),
        inference_fn=stubs.get("inference", fake_inf),
    )
    return orch, calls


# ---------------------------------------------------------------------------
# End-to-end via the fixture
# ---------------------------------------------------------------------------

def test_ingest_export_round_trip(tmp_path: Path) -> None:
    orch, infer_calls = _make_orch(tmp_path)
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert summary.parsed_videos == 8
    assert summary.parsed_events == 8
    assert summary.queued == 8
    assert summary.completed == 8
    assert summary.failed == 0

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM inference_runs").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM region_metrics").fetchone()[0] == 8 * len(REGION_IDS)
        # Every video marked downloaded with a local_path under data/videos.
        rows = conn.execute("SELECT downloaded, local_path FROM videos").fetchall()
        assert all(r[0] == 1 and r[1] is not None and r[1].endswith(".mp4") for r in rows)
        # Every job complete.
        statuses = [r[0] for r in conn.execute("SELECT status FROM pipeline_jobs").fetchall()]
        assert statuses == [STATUS_COMPLETE] * 8
    finally:
        conn.close()

    # The orchestrator threaded preprocessing params into extra_params for
    # every inference call so the reproducibility envelope is populated.
    assert len(infer_calls) == 8
    for call in infer_calls:
        for key, val in DEFAULT_PREPROCESSING_PARAMS.items():
            assert call["extra_params"][key] == val
        assert call["sample_rate_hz"] == pytest.approx(1.5)
        # Seeds are deterministic from video id.
        assert call["seed"] == _seed_for(call["video_id"])


def test_idempotent_rerun_is_a_noop(tmp_path: Path) -> None:
    orch, infer_calls = _make_orch(tmp_path)
    orch.ingest_export(FIXTURE)
    assert len(infer_calls) == 8

    # Second run: every job already complete → no inference invocations.
    second = orch.ingest_export(FIXTURE)
    orch.close()
    assert second.completed == 0
    assert second.failed == 0
    assert second.queued == 0
    assert len(infer_calls) == 8  # unchanged

    # And the side-effect side: artifact files still exactly 8.
    runs = list((tmp_path / "activations").glob("*.npz"))
    assert len(runs) == 8


# ---------------------------------------------------------------------------
# Post-inference cleanup (purge_after_inference)
# ---------------------------------------------------------------------------

def test_purge_after_inference_deletes_mp4s_keeps_activations(
    tmp_path: Path,
) -> None:
    """With purge_after_inference=True, raw + preprocessed mp4s are gone
    once a run completes, but every inference artifact survives and the
    SQLite catalog reflects that videos are no longer on disk."""
    orch, _ = _make_orch(tmp_path, purge_after_inference=True)
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert summary.completed == 8 and summary.failed == 0

    # Source mp4s gone.
    assert list((tmp_path / "videos").glob("*.mp4")) == []
    assert list((tmp_path / "videos_processed").glob("*.mp4")) == []

    # Activation outputs are intact — that's the whole point of the run.
    npzs = list((tmp_path / "activations").glob("*.npz"))
    sidecars = list((tmp_path / "activations").glob("*.meta.json"))
    payloads = list((tmp_path / "activations").glob("*.json"))
    assert len(npzs) == 8
    assert len(sidecars) == 8
    assert len(payloads) >= 8  # includes the per-run JSON payload sidecar

    # Catalog rows reflect "predicted, not on disk anymore".
    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        rows = conn.execute(
            "SELECT downloaded, local_path FROM videos"
        ).fetchall()
        assert all(r[0] == 0 and r[1] is None for r in rows)
        # region_metrics still there — purge only touches mp4s, not data.
        assert conn.execute(
            "SELECT COUNT(*) FROM region_metrics"
        ).fetchone()[0] == 8 * len(REGION_IDS)
    finally:
        conn.close()


def test_purge_disabled_by_default_keeps_cache(tmp_path: Path) -> None:
    """Sanity: without the opt-in flag, the dedup cache survives."""
    orch, _ = _make_orch(tmp_path)
    try:
        orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert len(list((tmp_path / "videos").glob("*.mp4"))) == 8
    assert len(list((tmp_path / "videos_processed").glob("*.mp4"))) == 8


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

def test_download_failure_marks_failed_and_retries(tmp_path: Path) -> None:
    fail_count = [0]

    def flaky(url: str, dest: Path, ua: str) -> None:
        fail_count[0] += 1
        raise RuntimeError("boom")

    orch, _ = _make_orch(tmp_path, fetch=flaky)
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert summary.completed == 0
    assert summary.failed == 8
    # Default max_attempts=5 → 5 attempts per video × 8 videos.
    assert fail_count[0] == 5 * 8

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        statuses = [r[0] for r in conn.execute("SELECT status FROM pipeline_jobs").fetchall()]
        assert statuses == [STATUS_FAILED] * 8
        errs = [r[0] for r in conn.execute("SELECT last_error FROM pipeline_jobs").fetchall()]
        assert all(e and "after 5 attempts" in e for e in errs)
    finally:
        conn.close()


def test_failed_jobs_can_recover_on_retry(tmp_path: Path) -> None:
    # First run with failing fetch.
    orch_a, _ = _make_orch(tmp_path, fetch=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    orch_a.ingest_export(FIXTURE)
    orch_a.close()

    # Second run with working fetch — same db, but everything is FAILED.
    orch_b, calls = _make_orch(tmp_path)  # default fakes succeed
    summary = orch_b.run_pending()
    orch_b.close()

    assert summary.completed == 8
    assert summary.failed == 0
    assert len(calls) == 8


def test_resume_from_partial_state(tmp_path: Path) -> None:
    """Crash after download → restart picks up at preprocess."""
    # Round 1: succeed download, then fail ffmpeg on every video.
    orch_a, _ = _make_orch(
        tmp_path,
        ffmpeg=lambda args: (_ for _ in ()).throw(RuntimeError("ffmpeg crashed")),
    )
    summary_a = orch_a.ingest_export(FIXTURE)
    orch_a.close()
    assert summary_a.completed == 0
    assert summary_a.failed == 8

    # Round 2: real fakes — should NOT redownload (cache hit), should
    # preprocess + infer.
    def boom_fetch(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("fetch must not be called when files cached")

    orch_b, calls = _make_orch(tmp_path, fetch=boom_fetch)
    summary_b = orch_b.run_pending()
    orch_b.close()

    assert summary_b.completed == 8
    assert summary_b.failed == 0
    assert len(calls) == 8


# ---------------------------------------------------------------------------
# Schema is what we documented
# ---------------------------------------------------------------------------

def test_schema_has_expected_tables(tmp_path: Path) -> None:
    orch, _ = _make_orch(tmp_path)
    orch.close()

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = {r[0] for r in rows}
        assert {"videos", "watch_events", "inference_runs",
                "region_metrics", "pipeline_jobs"}.issubset(tables)
    finally:
        conn.close()


def test_seed_for_is_deterministic() -> None:
    s = _seed_for("abc-123")
    assert s == _seed_for("abc-123")
    assert 0 <= s < 2 ** 31


# ---------------------------------------------------------------------------
# Robustness: corrupted DB + ffprobe failure
# ---------------------------------------------------------------------------

def test_corrupt_sqlite_db_raises_clear_error(tmp_path: Path) -> None:
    """A pre-existing file at db_path that isn't a SQLite database yields a
    clear, actionable RuntimeError instead of the cryptic sqlite3
    'file is not a database'."""
    db_path = tmp_path / "sqlite" / "neural_media.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"NOT A SQLITE DATABASE -- just some garbage bytes\x00\x01" * 8)

    cfg = OrchestratorConfig(
        db_path=db_path,
        videos_dir=tmp_path / "videos",
        processed_dir=tmp_path / "videos_processed",
        activations_dir=tmp_path / "activations",
    )
    with pytest.raises(RuntimeError, match="corrupted or not a database"):
        Orchestrator(cfg)


def test_ffprobe_failure_logs_debug_and_returns_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing ffprobe is logged at DEBUG and falls back to the default
    duration — it never propagates out of _resolve_duration_s."""

    def boom_probe(_path: Path) -> float:
        raise RuntimeError("ffprobe exited 1")

    orch, _ = _make_orch(tmp_path, probe=boom_probe)
    try:
        video = VideoMetadata(
            id="vid-x",
            source_url="https://www.tiktok.com/@a/video/vid-x",
            duration_s=0.0,
        )
        orch._upsert_videos([video])
        orch._enqueue_pending([video])
        # Pretend preprocessing produced a file so the probe branch is taken.
        orch._update_job("vid-x", preprocessed_path=str(tmp_path / "p.mp4"))

        with caplog.at_level(logging.DEBUG, logger="neural_media_pipeline.orchestrate"):
            duration = orch._resolve_duration_s(video)
    finally:
        orch.close()

    # Fell back to the configured default rather than raising.
    assert duration == orch.cfg.default_duration_s
    assert any(
        "ffprobe failed" in rec.getMessage() and rec.levelno == logging.DEBUG
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Round-trip through Pydantic (schemas the api worker will read)
# ---------------------------------------------------------------------------

def test_real_run_inference_integration(tmp_path: Path) -> None:
    """One end-to-end run with the REAL run_inference + MockBackend.

    Catches drift in the call signature (duration_s/seed/sample_rate_hz/
    extra_params) and confirms our DB persists what the api worker will
    later read.
    """
    cfg = OrchestratorConfig(
        db_path=tmp_path / "sqlite" / "neural_media.db",
        videos_dir=tmp_path / "videos",
        processed_dir=tmp_path / "videos_processed",
        activations_dir=tmp_path / "activations",
    )
    orch = Orchestrator(
        cfg,
        fetch=_fake_fetch_writes_stub,
        ffmpeg=_fake_ffmpeg_writes_stub,
        sleep=lambda _s: None,
        probe_duration=_fake_probe,
        # default inference_fn = real run_inference
    )
    try:
        # Just one video to keep runtime tight.
        from neural_media_pipeline.importer import parse_export as _parse
        videos, events = _parse(FIXTURE)
        orch._upsert_videos(videos[:1])
        orch._upsert_watch_events([e for e in events if e.video_id == videos[0].id])
        orch._enqueue_pending(videos[:1])
        summary = orch.run_pending()
    finally:
        orch.close()

    assert summary.completed == 1
    # Mock backend produced an npz + sidecar + downsampled JSON.
    assert any(p.suffix == ".npz" for p in (tmp_path / "activations").iterdir())
    assert any(p.name.endswith(".meta.json") for p in (tmp_path / "activations").iterdir())
    assert any(p.suffix == ".json" and not p.name.endswith(".meta.json")
               for p in (tmp_path / "activations").iterdir())


# ---------------------------------------------------------------------------
# Tier-b cleanup (purge_activations)
# ---------------------------------------------------------------------------

def test_purge_activations_deletes_npz_meta_payload_keeps_metrics(
    tmp_path: Path,
) -> None:
    """purge_activations=True drops the per-vertex blobs but the irreducible
    region_metrics rows survive and inference_runs.activation_path is
    cleared so the api can fall through to the no-payload path."""
    orch, _ = _make_orch(tmp_path, purge_activations=True)
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert summary.completed == 8 and summary.failed == 0

    # Activation blobs gone.
    assert list((tmp_path / "activations").glob("*.npz")) == []
    assert list((tmp_path / "activations").glob("*.meta.json")) == []
    # The per-run downsampled JSON payload sits next to the npz; only it
    # is named ``<run_id>.json``. Anything else .json in that dir would
    # be a bug.
    leftover_json = list((tmp_path / "activations").glob("*.json"))
    assert leftover_json == [], leftover_json

    # mp4s remain (purge_activations is independent of purge_after_inference).
    assert len(list((tmp_path / "videos").glob("*.mp4"))) == 8
    assert len(list((tmp_path / "videos_processed").glob("*.mp4"))) == 8

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        # region_metrics survives — that's the whole point of this tier.
        assert conn.execute(
            "SELECT COUNT(*) FROM region_metrics"
        ).fetchone()[0] == 8 * len(REGION_IDS)
        # activation_path cleared on every run so /videos/{id}/activation
        # can detect the purged state.
        paths = [r[0] for r in conn.execute(
            "SELECT activation_path FROM inference_runs"
        ).fetchall()]
        assert paths == [""] * 8
    finally:
        conn.close()


def test_purge_activations_and_mp4s_can_run_together(tmp_path: Path) -> None:
    """Both tiers on: every byte the pipeline wrote to disk is gone, only
    SQLite rows remain."""
    orch, _ = _make_orch(
        tmp_path, purge_after_inference=True, purge_activations=True,
    )
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert summary.completed == 8
    for sub in ("videos", "videos_processed", "activations"):
        assert list((tmp_path / sub).rglob("*")) == [], sub

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM region_metrics"
        ).fetchone()[0] == 8 * len(REGION_IDS)
    finally:
        conn.close()


def test_purge_activations_disabled_by_default_keeps_artifacts(
    tmp_path: Path,
) -> None:
    orch, _ = _make_orch(tmp_path)
    try:
        orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    assert len(list((tmp_path / "activations").glob("*.npz"))) == 8
    assert len(list((tmp_path / "activations").glob("*.meta.json"))) == 8


def test_purge_activations_swallows_filesystem_errors(tmp_path: Path) -> None:
    """A failing unlink (e.g. EACCES) is logged but never fails the run.

    Mirrors the contract of _purge_video_artifacts: the inference result
    is already durable in SQLite by the time cleanup runs, so cleanup
    must not be the thing that flips the job to FAILED.
    """
    orch, _ = _make_orch(tmp_path, purge_activations=True)
    # Replace Path.unlink with a raiser for activation paths only.
    original = Path.unlink

    def boom_unlink(self, *args, **kwargs):
        if self.suffix in (".npz", ".json"):
            raise OSError("simulated EACCES")
        return original(self, *args, **kwargs)

    try:
        Path.unlink = boom_unlink  # type: ignore[assignment]
        summary = orch.ingest_export(FIXTURE)
    finally:
        Path.unlink = original  # type: ignore[assignment]
        orch.close()

    # Still complete — cleanup failures are advisory.
    assert summary.completed == 8 and summary.failed == 0
    # Files still there because the patched unlink refused — that's fine,
    # the test is about not crashing.
    assert len(list((tmp_path / "activations").glob("*.npz"))) == 8


def test_persisted_rows_round_trip_through_schemas(tmp_path: Path) -> None:
    orch, _ = _make_orch(tmp_path)
    try:
        orch.ingest_export(FIXTURE)
    finally:
        orch.close()

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute("SELECT * FROM videos"):
            import json as _json
            VideoMetadata(
                id=row["id"], source_url=row["source_url"],
                title=row["title"], author=row["author"],
                duration_s=row["duration_s"], downloaded=bool(row["downloaded"]),
                local_path=row["local_path"], tags=_json.loads(row["tags_json"]),
            )
        for row in conn.execute("SELECT * FROM inference_runs"):
            import json as _json
            InferenceRun(
                id=row["id"], video_id=row["video_id"],
                model_id=row["model_id"], model_version=row["model_version"],
                seed=row["seed"], params_json=_json.loads(row["params_json"]),
                created_at=row["created_at"], activation_path=row["activation_path"],
                status=row["status"],
            )
    finally:
        conn.close()
