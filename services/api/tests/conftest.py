"""Test fixtures.

`sample_root` builds the on-disk layout produced by
`services/inference/scripts/build_sample_outputs.py`:

    mock_inference/
        index.json
        {video_id}.run.json
        {video_id}.metrics.json
        {video_id}.activation.json

`tiktok_export_path` writes a small TikTok export covering the same URLs
so SampleStore can derive watch events via `neural_media_pipeline.parse_export`.

`populated_sqlite_db` initializes a SQLite catalog via `init_db()` and
seeds it with the same dataset, so SampleStore and SqliteStore tests
compare against the same ground truth.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from neural_media_api.sqlite_store import init_db
from neural_media_pipeline import stable_video_id, stable_watch_event_id
from shared.schemas import NUM_VERTICES


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

_REGIONS = ("v1", "v2", "v3", "v4", "auditory", "language", "ffa", "vwfa")
_NUM_TIMEPOINTS = 6
_SAMPLE_RATE_HZ = 1.5
_MODEL_ID = "tribe-v2-mock"
_MODEL_VERSION = "0.0.0-mock"
_SEED = 7

# Hand-crafted sample: two videos, three watch events distributed across
# hours / days so the aggregator's hour-of-day / day-of-week buckets are
# non-trivial.
_VIDEOS = [
    {
        "source_url": "https://www.tiktok.com/@sampleuser/video/9000000000000000001",
        "duration_s": 12.0,
        "watched_at": ["2026-05-12 08:14:03", "2026-05-13 12:30:11"],
    },
    {
        "source_url": "https://www.tiktok.com/@chefcorpus/video/9000000000000000002",
        "duration_s": 18.0,
        "watched_at": ["2026-05-12 22:01:08"],
    },
]


def _video_id(v: dict) -> str:
    return stable_video_id(v["source_url"])


def _run_id(v: dict) -> str:
    return f"run-{_video_id(v)[:8]}"


def _metrics_rows(v: dict) -> list[dict]:
    """One RegionMetrics row per region for a single video."""
    vid = _video_id(v)
    rows = []
    for i, region in enumerate(_REGIONS):
        base = 0.1 + 0.05 * i
        ts = [round(base + 0.01 * t, 4) for t in range(_NUM_TIMEPOINTS)]
        rows.append({
            "region_id": region,
            "video_id": vid,
            "inference_run_id": _run_id(v),
            "mean": round(sum(ts) / len(ts), 4),
            "peak": max(ts),
            "sustained": round(min(ts), 4),
            "timeseries": ts,
        })
    return rows


def _activation_payload(v: dict) -> dict:
    vid = _video_id(v)
    timestamps = [round(t / _SAMPLE_RATE_HZ, 4) for t in range(_NUM_TIMEPOINTS)]
    region_means = {
        region: [round(0.1 + 0.05 * i + 0.01 * t, 4) for t in range(_NUM_TIMEPOINTS)]
        for i, region in enumerate(_REGIONS)
    }
    keyframe_vertices = {"0.000": [0.0, 0.1, 0.2], "3.000": [0.05, 0.15, 0.25]}
    return {
        "inference_run_id": _run_id(v),
        "video_id": vid,
        "num_vertices": NUM_VERTICES,
        "num_timepoints": _NUM_TIMEPOINTS,
        "sample_rate_hz": _SAMPLE_RATE_HZ,
        "timestamps": timestamps,
        "keyframe_vertices": keyframe_vertices,
        "region_means": region_means,
    }


def _run_row(v: dict, activation_path: str) -> dict:
    return {
        "id": _run_id(v),
        "video_id": _video_id(v),
        "model_id": _MODEL_ID,
        "model_version": _MODEL_VERSION,
        "seed": _SEED,
        "params_json": {
            "duration_s": v["duration_s"],
            "sample_rate_hz": _SAMPLE_RATE_HZ,
            "num_timepoints": _NUM_TIMEPOINTS,
        },
        "created_at": "2026-05-15T00:00:00+00:00",
        "activation_path": activation_path,
        "status": "complete",
    }


def _index() -> dict:
    return {
        "model_id": _MODEL_ID,
        "model_version": _MODEL_VERSION,
        "seed": _SEED,
        "sample_rate_hz": _SAMPLE_RATE_HZ,
        "videos": [
            {
                "video_id": _video_id(v),
                "source_url": v["source_url"],
                "inference_run_id": _run_id(v),
                "duration_s": v["duration_s"],
                "num_timepoints": _NUM_TIMEPOINTS,
            }
            for v in _VIDEOS
        ],
    }


def _tiktok_export() -> dict:
    """The shape `neural_media_pipeline.parse_export` reads."""
    video_list = []
    for v in _VIDEOS:
        for ts in v["watched_at"]:
            video_list.append({"Date": ts, "Link": v["source_url"]})
    return {
        "Activity": {
            "Video Browsing History": {"VideoList": video_list},
        }
    }


# --- Public fixtures -------------------------------------------------------


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    """Populated mock_inference/ tree in tmp_path."""
    root = tmp_path / "mock_inference"
    root.mkdir()
    (root / "index.json").write_text(json.dumps(_index()))
    for v in _VIDEOS:
        vid = _video_id(v)
        # activation_path on the run row points back at the JSON wire
        # file because the sample fixture has no .npz.
        activation_json = root / f"{vid}.activation.json"
        activation_json.write_text(json.dumps(_activation_payload(v)))
        (root / f"{vid}.metrics.json").write_text(json.dumps(_metrics_rows(v)))
        (root / f"{vid}.run.json").write_text(json.dumps(
            _run_row(v, activation_path=str(activation_json))
        ))
    return root


@pytest.fixture
def tiktok_export_path(tmp_path: Path) -> Path:
    path = tmp_path / "user_data.json"
    path.write_text(json.dumps(_tiktok_export()))
    return path


@pytest.fixture
def empty_sample_root(tmp_path: Path) -> Path:
    root = tmp_path / "empty_mock"
    root.mkdir()
    return root


@pytest.fixture
def populated_video_ids() -> list[str]:
    return [_video_id(v) for v in _VIDEOS]


@pytest.fixture
def populated_regions() -> tuple[str, ...]:
    return _REGIONS


@pytest.fixture
def populated_sqlite_db(tmp_path: Path) -> Path:
    """A SQLite catalog initialized + seeded with the same dataset.

    The activation file is a real (small) NPZ so SqliteStore.get_activation
    exercises the full numpy -> wire-format path.
    """
    db_path = tmp_path / "neural_media.db"
    init_db(db_path)

    activations_dir = tmp_path / "activations"
    activations_dir.mkdir()

    conn = sqlite3.connect(db_path)
    try:
        for idx, v in enumerate(_VIDEOS):
            vid = _video_id(v)
            conn.execute(
                "INSERT INTO videos "
                "(id, source_url, title, author, duration_s, downloaded, "
                " local_path, tags_json, first_seen_idx) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (vid, v["source_url"], None, None, v["duration_s"], 0, None, "[]", idx),
            )
            for ts in v["watched_at"]:
                dt = _parse_ts(ts)
                event_id = stable_watch_event_id(vid, dt)
                conn.execute(
                    "INSERT INTO watch_events "
                    "(id, video_id, watched_at, duration_watched_s, "
                    " completion_pct, source) "
                    "VALUES (?,?,?,?,?,?)",
                    (event_id, vid, dt.isoformat(), v["duration_s"], 1.0, "tiktok_export"),
                )

            # Real NPZ so get_activation can downsample it.
            rng = np.random.default_rng(seed=hash(vid) & 0xFFFF)
            arr = rng.random(size=(_NUM_TIMEPOINTS, NUM_VERTICES), dtype=np.float32)
            activation_path = activations_dir / f"{_run_id(v)}.npz"
            np.savez_compressed(activation_path, activations=arr)

            run_row = _run_row(v, activation_path=str(activation_path))
            conn.execute(
                "INSERT INTO inference_runs "
                "(id, video_id, model_id, model_version, seed, params_json, "
                " created_at, activation_path, status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_row["id"],
                    run_row["video_id"],
                    run_row["model_id"],
                    run_row["model_version"],
                    run_row["seed"],
                    json.dumps(run_row["params_json"]),
                    run_row["created_at"],
                    run_row["activation_path"],
                    run_row["status"],
                ),
            )
            for m in _metrics_rows(v):
                conn.execute(
                    "INSERT INTO region_metrics "
                    "(inference_run_id, region_id, video_id, mean, peak, "
                    " sustained, timeseries_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        m["inference_run_id"],
                        m["region_id"],
                        m["video_id"],
                        m["mean"],
                        m["peak"],
                        m["sustained"],
                        json.dumps(m["timeseries"]),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path
