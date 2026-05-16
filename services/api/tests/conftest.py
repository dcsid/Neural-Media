"""Test fixtures.

The `sample_root` fixture builds a self-contained `data/sample/mock_inference`
layout inside a tmp dir. It writes the JSON shapes that `SampleStore`
expects, and uses values that pass `shared.schemas` validation so any test
that round-trips through Pydantic stays honest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Two videos × eight regions × a small timeseries, plus three watch events.
_VIDEO_IDS = ["vid-aaaa", "vid-bbbb"]
_REGIONS = ("v1", "v2", "v3", "v4", "auditory", "language", "ffa", "vwfa")
_NUM_TIMEPOINTS = 6
_SAMPLE_RATE_HZ = 1.5


def _videos() -> list[dict]:
    return [
        {
            "id": "vid-aaaa",
            "source_url": "https://www.tiktok.com/@sampleuser/video/1",
            "title": "Sample A",
            "author": "sampleuser",
            "duration_s": 12.0,
            "downloaded": False,
            "local_path": None,
            "tags": ["food"],
        },
        {
            "id": "vid-bbbb",
            "source_url": "https://www.tiktok.com/@sampleuser/video/2",
            "title": "Sample B",
            "author": "sampleuser",
            "duration_s": 18.0,
            "downloaded": False,
            "local_path": None,
            "tags": ["travel"],
        },
    ]


def _watch_events() -> list[dict]:
    return [
        {
            "id": "we-1",
            "video_id": "vid-aaaa",
            "watched_at": "2026-05-12T08:14:03+00:00",
            "duration_watched_s": 9.5,
            "completion_pct": 0.79,
            "source": "tiktok_export",
        },
        {
            "id": "we-2",
            "video_id": "vid-bbbb",
            "watched_at": "2026-05-12T22:01:08+00:00",
            "duration_watched_s": 17.0,
            "completion_pct": 0.94,
            "source": "tiktok_export",
        },
        {
            "id": "we-3",
            "video_id": "vid-aaaa",
            "watched_at": "2026-05-13T12:30:11+00:00",
            "duration_watched_s": 12.0,
            "completion_pct": 1.0,
            "source": "tiktok_export",
        },
    ]


def _inference_runs() -> list[dict]:
    return [
        {
            "id": f"run-{vid}",
            "video_id": vid,
            "model_id": "tribe-v2-mock",
            "model_version": "0.0.0-mock",
            "seed": 7,
            "params_json": {"resolution": "224x224", "fps": 1, "audio_sr": 16000},
            "created_at": "2026-05-15T00:00:00+00:00",
            "activation_path": f"data/sample/mock_inference/activations/run-{vid}.json",
            "status": "complete",
        }
        for vid in _VIDEO_IDS
    ]


def _region_metrics() -> list[dict]:
    rows = []
    for vid in _VIDEO_IDS:
        for i, region in enumerate(_REGIONS):
            base = 0.1 + 0.05 * i
            ts = [round(base + 0.01 * t, 4) for t in range(_NUM_TIMEPOINTS)]
            rows.append({
                "region_id": region,
                "video_id": vid,
                "inference_run_id": f"run-{vid}",
                "mean": round(sum(ts) / len(ts), 4),
                "peak": max(ts),
                "sustained": round(min(ts), 4),
                "timeseries": ts,
            })
    return rows


def _activation(video_id: str) -> dict:
    timestamps = [round(t / _SAMPLE_RATE_HZ, 4) for t in range(_NUM_TIMEPOINTS)]
    region_means = {
        region: [round(0.1 + 0.05 * i + 0.01 * t, 4) for t in range(_NUM_TIMEPOINTS)]
        for i, region in enumerate(_REGIONS)
    }
    # Keyframe vertices are sparse — two timepoints, each with a tiny
    # placeholder vector. Real outputs would have 20484 floats per keyframe.
    keyframe_vertices = {
        "0": [0.0, 0.1, 0.2],
        "3": [0.05, 0.15, 0.25],
    }
    return {
        "inference_run_id": f"run-{video_id}",
        "video_id": video_id,
        "num_vertices": 20484,
        "num_timepoints": _NUM_TIMEPOINTS,
        "sample_rate_hz": _SAMPLE_RATE_HZ,
        "timestamps": timestamps,
        "keyframe_vertices": keyframe_vertices,
        "region_means": region_means,
    }


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    """Write a populated `mock_inference/` tree into `tmp_path`."""
    root = tmp_path / "mock_inference"
    root.mkdir()
    (root / "videos.json").write_text(json.dumps(_videos()))
    (root / "watch_events.json").write_text(json.dumps(_watch_events()))
    (root / "inference_runs.json").write_text(json.dumps(_inference_runs()))
    (root / "region_metrics.json").write_text(json.dumps(_region_metrics()))
    activations = root / "activations"
    activations.mkdir()
    for vid in _VIDEO_IDS:
        (activations / f"run-{vid}.json").write_text(json.dumps(_activation(vid)))
    return root


@pytest.fixture
def empty_sample_root(tmp_path: Path) -> Path:
    """An empty (but existing) sample root — exercises the missing-file path."""
    root = tmp_path / "empty"
    root.mkdir()
    return root


@pytest.fixture
def populated_video_ids() -> list[str]:
    return list(_VIDEO_IDS)


@pytest.fixture
def populated_regions() -> tuple[str, ...]:
    return _REGIONS
