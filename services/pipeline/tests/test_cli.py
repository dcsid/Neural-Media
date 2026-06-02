"""CLI smoke tests. Drives main() with stubbed orchestrator side effects."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from neural_media_inference import RunArtifacts

from neural_media_pipeline import cli
from neural_media_pipeline.orchestrate import _seed_for
from shared.schemas import (
    NUM_VERTICES,
    REGION_IDS,
    ActivationOutput,
    InferenceRun,
    RegionMetrics,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "data" / "sample" / "tiktok_export" / "user_data.json"


def _patched_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``cli.Orchestrator`` with one whose side effects are stubbed."""

    def _fake_fetch(url, dest, ua):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")

    def _fake_ffmpeg(args):
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"y")

    def _fake_probe(_):
        return 12.0

    def _fake_inference(*, video_id, duration_s, seed, sample_rate_hz,
                        activations_dir, extra_params=None, **kw):
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

    real = cli.Orchestrator

    def _factory(cfg):
        return real(
            cfg,
            fetch=_fake_fetch, ffmpeg=_fake_ffmpeg,
            sleep=lambda _s: None, probe_duration=_fake_probe,
            inference_fn=_fake_inference,
        )

    monkeypatch.setattr(cli, "Orchestrator", _factory)


def test_cli_ingests_fixture(monkeypatch: pytest.MonkeyPatch,
                             tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _patched_orchestrator(monkeypatch)

    rc = cli.main([
        str(FIXTURE),
        "--data-root", str(tmp_path),
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert "parsed: 8 videos, 8 events" in out
    assert "completed: 8" in out
    assert "failed: 0" in out

    db = tmp_path / "sqlite" / "neural_media.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 8
    finally:
        conn.close()


def test_cli_dry_run_does_not_create_db(tmp_path: Path,
                                        capsys: pytest.CaptureFixture) -> None:
    rc = cli.main([str(FIXTURE), "--data-root", str(tmp_path), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "parsed: 8 videos, 8 events" in out
    assert not (tmp_path / "sqlite" / "neural_media.db").exists()


def test_cli_missing_export(tmp_path: Path,
                            capsys: pytest.CaptureFixture) -> None:
    rc = cli.main([str(tmp_path / "does-not-exist.json"), "--data-root", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_cli_neither_export_nor_run_pending(tmp_path: Path,
                                            capsys: pytest.CaptureFixture) -> None:
    rc = cli.main(["--data-root", str(tmp_path)])
    assert rc == 2
    assert "--run-pending" in capsys.readouterr().err


def test_cli_run_pending_uses_existing_db(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path,
                                          capsys: pytest.CaptureFixture) -> None:
    _patched_orchestrator(monkeypatch)
    # First, populate the db.
    assert cli.main([str(FIXTURE), "--data-root", str(tmp_path)]) == 0
    capsys.readouterr()

    # Now drive run-pending — every job already complete, should be a no-op.
    rc = cli.main(["--run-pending", "--data-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "completed: 0" in out
    assert "failed: 0" in out


def test_cli_partial_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A PARTIAL run (some videos completed AND some failed) exits non-zero.

    Regression: the old ``failed and not completed`` guard returned 0 as
    soon as a single video completed, hiding partial failures from CI and
    shell scripts that gate on the exit status.
    """

    class _PartialOrch:
        def __init__(self, cfg):
            self.cfg = cfg

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def run(self, _export_path, *, since=None, until=None):
            return cli.IngestSummary(
                parsed_videos=8, parsed_events=8, queued=8,
                completed=7, failed=1, errors=[("vid-x", "boom")],
            )

        def close(self):
            pass

    monkeypatch.setattr(cli, "Orchestrator", _PartialOrch)
    rc = cli.main([str(FIXTURE), "--data-root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "completed: 7; failed: 1" in out


def test_cli_dry_run_without_export_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """``--dry-run`` requires an export. Combined with ``--run-pending``
    (which makes the export optional) it must error cleanly with exit 2
    rather than crashing inside ``parse_export(None)``.
    """
    rc = cli.main(["--run-pending", "--dry-run", "--data-root", str(tmp_path)])
    assert rc == 2
    assert "--dry-run requires an export" in capsys.readouterr().err


def test_cli_output_never_leaks_full_url(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path,
                                         capsys: pytest.CaptureFixture) -> None:
    """Even when jobs fail, only video ids appear in the output."""

    def boom_fetch(url, dest, ua):
        raise RuntimeError("ratelimited")

    real = cli.Orchestrator

    def factory(cfg):
        return real(
            cfg,
            fetch=boom_fetch,
            ffmpeg=lambda args: None,
            sleep=lambda _s: None,
            probe_duration=lambda p: 12.0,
        )

    monkeypatch.setattr(cli, "Orchestrator", factory)
    cli.main([str(FIXTURE), "--data-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert "https://www.tiktok.com" not in captured.out
    assert "https://www.tiktok.com" not in captured.err
    # video ids are uuid5 hashes of the URL — those ARE in the output and
    # that's by design (they're the safe handle for failure follow-up).
    seed_for_sample = _seed_for  # noqa: F841 — touch import for clarity


def test_dunder_main_module_runs() -> None:
    """``python -m neural_media_pipeline`` should resolve to the CLI."""
    import importlib.util

    spec = importlib.util.find_spec("neural_media_pipeline.__main__")
    assert spec is not None and spec.origin is not None


# ---------------------------------------------------------------------------
# Time-window flag precedence (--minutes > --hours > --days > --since)
# ---------------------------------------------------------------------------

def _parse(argv: list[str]) -> object:
    return cli._build_parser().parse_args(argv)


def test_resolve_since_minutes_beats_hours_days_since() -> None:
    args = _parse([
        "fixture.json",
        "--since", "2020-01-01",
        "--days", "30",
        "--hours", "12",
        "--minutes", "5",
    ])
    now = datetime.now(timezone.utc)
    cutoff = cli._resolve_since(args)
    assert cutoff is not None
    delta = (now - cutoff).total_seconds()
    # Should be ~5 minutes (300 s). Allow a 2-second window for clock jitter.
    assert 298.0 < delta < 302.0


def test_resolve_since_hours_beats_days_and_since() -> None:
    args = _parse([
        "fixture.json",
        "--since", "2020-01-01",
        "--days", "30",
        "--hours", "2",
    ])
    cutoff = cli._resolve_since(args)
    assert cutoff is not None
    delta = (datetime.now(timezone.utc) - cutoff).total_seconds()
    assert 7195.0 < delta < 7205.0  # 2h ± 5s


def test_resolve_since_days_beats_explicit_since() -> None:
    args = _parse(["fixture.json", "--since", "2020-01-01", "--days", "7"])
    cutoff = cli._resolve_since(args)
    assert cutoff is not None
    # If --days won, the cutoff is recent — anything within the last day.
    age = (datetime.now(timezone.utc) - cutoff).total_seconds()
    assert 7 * 86400 - 5 < age < 7 * 86400 + 5


def test_resolve_since_falls_back_to_explicit_since() -> None:
    args = _parse(["fixture.json", "--since", "2024-06-01"])
    cutoff = cli._resolve_since(args)
    assert cutoff == datetime(2024, 6, 1, tzinfo=timezone.utc)


def test_resolve_since_none_when_unset() -> None:
    args = _parse(["fixture.json"])
    assert cli._resolve_since(args) is None


def test_cli_dry_run_minutes_filters_old_history(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """--minutes 1 on a fixture with only ancient events parses 0 events."""
    rc = cli.main([
        str(FIXTURE),
        "--data-root", str(tmp_path),
        "--dry-run",
        "--minutes", "1",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # FIXTURE's events are from 2026-05-12 onwards; --minutes 1 from now
    # excludes them all. parse_export still emits 0 videos because no
    # event survives the window.
    assert "parsed: 0 videos, 0 events" in out


def test_cli_purge_activations_threads_through_to_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``--purge-activations`` must reach ``OrchestratorConfig`` unchanged."""
    captured: dict = {}

    real = cli.Orchestrator

    def factory(cfg):
        captured["cfg"] = cfg
        return real(
            cfg,
            fetch=lambda *a, **k: (a[1].parent.mkdir(parents=True, exist_ok=True)
                                   or a[1].write_bytes(b"x")),
            ffmpeg=lambda args: (Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                                 or Path(args[-1]).write_bytes(b"y")),
            sleep=lambda _s: None,
            probe_duration=lambda _p: 12.0,
        )

    monkeypatch.setattr(cli, "Orchestrator", factory)
    rc = cli.main([
        str(FIXTURE),
        "--data-root", str(tmp_path),
        "--purge-activations",
        # also pass --mock so we don't depend on ffmpeg/yt-dlp here.
        "--mock",
    ])
    assert rc == 0
    assert captured["cfg"].purge_activations is True
    assert captured["cfg"].purge_after_inference is False
