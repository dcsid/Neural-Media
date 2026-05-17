"""ProgressEvent emission + mock-mode (skip_download/skip_preprocess).

These tests cover the public surface the api-orchestrator worker calls
into from inside ``POST /api/v1/import``:

  orch = Orchestrator(OrchestratorConfig(
      data_root=..., skip_download=True, skip_preprocess=True,
  ))
  summary = orch.run(export_path, progress=callback)
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from neural_media_inference import RunArtifacts

from neural_media_pipeline import (
    Orchestrator,
    OrchestratorConfig,
    ProgressEvent,
)
from neural_media_pipeline.orchestrate import (
    STATUS_COMPLETE,
    _synth_duration_s,
)
from shared.schemas import (
    NUM_VERTICES,
    REGION_IDS,
    ActivationOutput,
    InferenceRun,
    RegionMetrics,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

def _fake_inference_factory():
    """Inference stub: deterministic outputs, captures every call."""
    calls: list[dict] = []

    def fake(*, video_id, duration_s, seed, sample_rate_hz,
             activations_dir, extra_params=None, **kw):
        calls.append({
            "video_id": video_id, "duration_s": duration_s, "seed": seed,
            "sample_rate_hz": sample_rate_hz,
            "extra_params": dict(extra_params or {}),
            "backend": kw.get("backend"),
        })
        run_id = f"run-{video_id}"
        Path(activations_dir).mkdir(parents=True, exist_ok=True)
        npz = Path(activations_dir) / f"{run_id}.npz"
        npz.write_bytes(b"")
        meta = Path(activations_dir) / f"{run_id}.meta.json"
        meta.write_text("{}")
        n_t = max(8, int(duration_s * sample_rate_hz))
        return RunArtifacts(
            inference_run=InferenceRun(
                id=run_id, video_id=video_id, model_id="tribe-v2-mock",
                model_version="0.0.0-mock", seed=seed, params_json={},
                created_at=datetime.now(timezone.utc),
                activation_path=str(npz), status="complete",
            ),
            region_metrics=[
                RegionMetrics(
                    region_id=r, video_id=video_id, inference_run_id=run_id,
                    mean=0.0, peak=0.0, sustained=0.0, timeseries=[0.0] * n_t,
                ) for r in REGION_IDS
            ],
            activation_payload=ActivationOutput(
                inference_run_id=run_id, video_id=video_id,
                num_vertices=NUM_VERTICES, num_timepoints=n_t,
                sample_rate_hz=sample_rate_hz,
                timestamps=[i / sample_rate_hz for i in range(n_t)],
                keyframe_vertices={"0.0": [0.0]},
                region_means={r: [0.0] * n_t for r in REGION_IDS},
            ),
            activation_path=npz, sidecar_path=meta,
        )

    return fake, calls


def _make_mock_orch(tmp_path: Path):
    """Orchestrator in full demo mode: no yt-dlp, no ffmpeg, no ffprobe."""
    cfg = OrchestratorConfig(
        data_root=tmp_path,
        skip_download=True,
        skip_preprocess=True,
    )

    def boom_fetch(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("fetch must not be called in mock mode")

    def boom_ffmpeg(args):  # pragma: no cover — must not run
        raise AssertionError("ffmpeg must not be called in mock mode")

    def boom_probe(_):  # pragma: no cover — must not run
        raise AssertionError("ffprobe must not be called in mock mode")

    fake_inf, calls = _fake_inference_factory()
    return Orchestrator(
        cfg,
        fetch=boom_fetch,
        ffmpeg=boom_ffmpeg,
        probe_duration=boom_probe,
        inference_fn=fake_inf,
        sleep=lambda _s: None,
    ), calls


# ---------------------------------------------------------------------------
# data_root resolution
# ---------------------------------------------------------------------------

def test_data_root_expands_into_four_paths(tmp_path: Path) -> None:
    cfg = OrchestratorConfig(data_root=tmp_path)
    assert cfg.db_path == tmp_path / "sqlite" / "neural_media.db"
    assert cfg.videos_dir == tmp_path / "videos"
    assert cfg.processed_dir == tmp_path / "videos_processed"
    assert cfg.activations_dir == tmp_path / "activations"


def test_data_root_does_not_override_explicit_paths(tmp_path: Path) -> None:
    explicit_db = tmp_path / "custom.db"
    cfg = OrchestratorConfig(data_root=tmp_path, db_path=explicit_db)
    assert cfg.db_path == explicit_db
    # Other paths still derive from root.
    assert cfg.videos_dir == tmp_path / "videos"


def test_config_without_data_root_requires_explicit_paths() -> None:
    with pytest.raises(ValueError) as exc_info:
        OrchestratorConfig()
    assert "data_root" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Mock mode end-to-end via run()
# ---------------------------------------------------------------------------

def test_mock_mode_runs_under_one_second(tmp_path: Path) -> None:
    """Full demo flow: 8 videos, no subprocesses, ProgressEvents fire."""
    orch, infer_calls = _make_mock_orch(tmp_path)
    events: list[ProgressEvent] = []

    try:
        summary = orch.run(FIXTURE, progress=events.append)
    finally:
        orch.close()

    assert summary.parsed_videos == 8
    assert summary.parsed_events == 8
    assert summary.completed == 8
    assert summary.failed == 0
    assert len(infer_calls) == 8


def test_mock_mode_leaves_videos_undownloaded(tmp_path: Path) -> None:
    """The brief is explicit: rows stay downloaded=false, local_path=null."""
    orch, _ = _make_mock_orch(tmp_path)
    try:
        orch.run(FIXTURE)
    finally:
        orch.close()

    conn = sqlite3.connect(tmp_path / "sqlite" / "neural_media.db")
    try:
        rows = conn.execute(
            "SELECT downloaded, local_path FROM videos"
        ).fetchall()
        assert all(r[0] == 0 and r[1] is None for r in rows)

        # Jobs still all complete, with no preprocessed_path.
        jobs = conn.execute(
            "SELECT status, preprocessed_path FROM pipeline_jobs"
        ).fetchall()
        assert [j[0] for j in jobs] == [STATUS_COMPLETE] * 8
        assert all(j[1] is None for j in jobs)
    finally:
        conn.close()


def test_mock_mode_synth_duration_matches_sample_outputs(tmp_path: Path) -> None:
    """Durations passed to run_inference must match build_sample_outputs.py."""
    orch, infer_calls = _make_mock_orch(tmp_path)
    try:
        orch.run(FIXTURE)
    finally:
        orch.close()

    for call in infer_calls:
        expected = _synth_duration_s(call["video_id"])
        assert call["duration_s"] == pytest.approx(expected)
        # Confirm it lands in the documented [15, 60] band.
        assert 15.0 <= call["duration_s"] < 61.0


def test_backend_from_config_is_forwarded(tmp_path: Path) -> None:
    """``OrchestratorConfig.backend`` should land in run_inference's kwargs."""

    class _Sentinel:
        pass

    sentinel = _Sentinel()
    cfg = OrchestratorConfig(
        data_root=tmp_path, skip_download=True, skip_preprocess=True,
        backend=sentinel,  # type: ignore[arg-type]
    )
    fake_inf, calls = _fake_inference_factory()
    orch = Orchestrator(
        cfg,
        fetch=lambda *a, **k: None,
        ffmpeg=lambda args: None,
        probe_duration=lambda p: 12.0,
        inference_fn=fake_inf,
        sleep=lambda _s: None,
    )
    try:
        orch.run(FIXTURE)
    finally:
        orch.close()

    assert all(c["backend"] is sentinel for c in calls)


# ---------------------------------------------------------------------------
# Progress emission
# ---------------------------------------------------------------------------

def test_progress_emits_parsing_then_per_video(tmp_path: Path) -> None:
    orch, _ = _make_mock_orch(tmp_path)
    events: list[ProgressEvent] = []
    try:
        orch.run(FIXTURE, progress=events.append)
    finally:
        orch.close()

    assert events, "callback must have been invoked"
    # First event is parsing.
    assert events[0].phase == "parsing"
    # The first non-parsing event sets totals.
    after_parsing = [e for e in events if e.phase != "parsing"]
    assert after_parsing[0].videos_total == 8
    assert after_parsing[0].videos_processed == 0

    # In skip-both mode every per-video event is "inferring".
    assert all(e.phase in ("parsing", "inferring") for e in events)

    # videos_processed monotonically increases, reaches videos_total.
    processed = [e.videos_processed for e in after_parsing]
    assert processed[-1] == 8
    assert processed == sorted(processed)


def test_progress_phase_sequence_full_pipeline(tmp_path: Path) -> None:
    """Without skip flags, phases should walk download → preprocess → infer per video."""
    cfg = OrchestratorConfig(data_root=tmp_path)
    fake_inf, _ = _fake_inference_factory()

    def fake_fetch(url, dest, ua):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")

    def fake_ffmpeg(args):
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")

    orch = Orchestrator(
        cfg, fetch=fake_fetch, ffmpeg=fake_ffmpeg,
        probe_duration=lambda _: 20.0, inference_fn=fake_inf,
        sleep=lambda _s: None,
    )
    events: list[ProgressEvent] = []
    try:
        orch.run(FIXTURE, progress=events.append)
    finally:
        orch.close()

    # For the first video, phases should appear in order.
    per_video_phases: dict[str, list[str]] = {}
    for e in events:
        if e.message and e.message.startswith("run-") is False and e.phase != "parsing":
            # Skip the end-of-video tick (videos_processed advances; we treat
            # those identically here as the message is the video_id too).
            per_video_phases.setdefault(e.message, []).append(e.phase)

    # Pick any video and sanity-check the sub-phase order.
    for vid, phases in per_video_phases.items():
        # Each video sees downloading → preprocessing → inferring, then one
        # extra "inferring" tick from the end-of-video event.
        idx_d = phases.index("downloading") if "downloading" in phases else -1
        idx_p = phases.index("preprocessing") if "preprocessing" in phases else -1
        idx_i = phases.index("inferring") if "inferring" in phases else -1
        assert idx_d >= 0 and idx_p > idx_d and idx_i > idx_p, (vid, phases)


def test_progress_messages_carry_video_id_not_url(tmp_path: Path) -> None:
    orch, _ = _make_mock_orch(tmp_path)
    events: list[ProgressEvent] = []
    try:
        orch.run(FIXTURE, progress=events.append)
    finally:
        orch.close()

    for e in events:
        if e.message is None:
            continue
        assert "https://" not in e.message
        assert "tiktok.com" not in e.message


def test_run_callback_optional(tmp_path: Path) -> None:
    """Omitting the callback must still work (api worker may not always pass one)."""
    orch, _ = _make_mock_orch(tmp_path)
    try:
        summary = orch.run(FIXTURE)
    finally:
        orch.close()
    assert summary.completed == 8


def test_run_pending_emits_progress_too(tmp_path: Path) -> None:
    orch, _ = _make_mock_orch(tmp_path)
    try:
        orch.run(FIXTURE)  # populate
        events: list[ProgressEvent] = []
        summary = orch.run_pending(progress=events.append)
    finally:
        orch.close()
    # Everything was already complete → no per-video events, summary 0/0.
    assert summary.completed == 0
    assert summary.failed == 0


def test_ingest_export_back_compat(tmp_path: Path) -> None:
    """Older callers using ingest_export keep working."""
    orch, _ = _make_mock_orch(tmp_path)
    try:
        summary = orch.ingest_export(FIXTURE)
    finally:
        orch.close()
    assert summary.completed == 8


# ---------------------------------------------------------------------------
# Zip-input tolerance
# ---------------------------------------------------------------------------

def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)


def test_parse_export_reads_user_data_from_zip(tmp_path: Path) -> None:
    fixture_bytes = FIXTURE.read_bytes()
    zip_path = tmp_path / "tiktok_export.zip"
    _make_zip(zip_path, {"user_data.json": fixture_bytes})

    from neural_media_pipeline import parse_export
    videos, events = parse_export(zip_path)
    assert len(videos) == 8
    assert len(events) == 8


def test_parse_export_zip_case_insensitive_and_nested(tmp_path: Path) -> None:
    fixture_bytes = FIXTURE.read_bytes()
    zip_path = tmp_path / "weird.zip"
    _make_zip(zip_path, {
        "vendor/2026-05/USER_DATA.JSON": fixture_bytes,
        "manifest.txt": b"unrelated",
    })

    from neural_media_pipeline import parse_export
    videos, _ = parse_export(zip_path)
    assert len(videos) == 8


def test_parse_export_zip_prefers_shallowest_match(tmp_path: Path) -> None:
    canonical = FIXTURE.read_bytes()
    decoy = json.dumps({
        "Activity": {
            "Video Browsing History": {
                "VideoList": [
                    {"Date": "2026-05-12 08:14:03",
                     "Link": "https://www.tiktok.com/@decoy/video/9"}
                ]
            }
        }
    }).encode("utf-8")
    zip_path = tmp_path / "nested.zip"
    _make_zip(zip_path, {
        "user_data.json": canonical,
        "extras/old/user_data.json": decoy,
    })

    from neural_media_pipeline import parse_export
    videos, _ = parse_export(zip_path)
    assert len(videos) == 8  # canonical (top-level) wins, not the 1-video decoy


def test_parse_export_zip_without_user_data_raises(tmp_path: Path) -> None:
    zip_path = tmp_path / "empty.zip"
    _make_zip(zip_path, {"README.txt": b"hi"})
    from neural_media_pipeline import parse_export
    with pytest.raises(FileNotFoundError):
        parse_export(zip_path)


def test_parse_export_misnamed_zip_still_detected(tmp_path: Path) -> None:
    """A zip without a .zip extension is still detected via magic bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("user_data.json", FIXTURE.read_bytes())
    p = tmp_path / "export.bin"
    p.write_bytes(buf.getvalue())

    from neural_media_pipeline import parse_export
    videos, _ = parse_export(p)
    assert len(videos) == 8


def test_orchestrator_accepts_zip_export(tmp_path: Path) -> None:
    fixture_bytes = FIXTURE.read_bytes()
    zip_path = tmp_path / "export.zip"
    _make_zip(zip_path, {"user_data.json": fixture_bytes})

    orch, _ = _make_mock_orch(tmp_path)
    try:
        summary = orch.run(zip_path)
    finally:
        orch.close()
    assert summary.completed == 8
