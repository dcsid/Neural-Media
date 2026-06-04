"""Mock-mode test for ``scripts/smoke_gpu_clip.py``.

Stubs the production pipeline (``predict_one_url._run_pipeline``) so the
smoke runner is exercised end-to-end — shape summary + exit codes — with no
GPU, torch, network, or weights. This is what makes the pre-bake smoke
CI-runnable.

The script lives at the repo root and is loaded by path (it imports
``predict_one_url``, which its own sys.path shim puts on the path).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from neural_media_inference._shared import REGION_IDS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

_VALID_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _load_script(name: str):
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def smoke():
    return _load_script("smoke_gpu_clip")


def _canned_payload(n_t: int = 8) -> dict:
    return {
        "videoDurationSec": 5.0,
        "timestamps": [round(i / 1.5, 3) for i in range(n_t)],
        "byRegion": {r: [0.1] * n_t for r in REGION_IDS},
        "modelVersion": "tribe-v2-mock",
    }


def test_run_smoke_reports_shape(smoke, monkeypatch) -> None:
    import predict_one_url

    monkeypatch.setattr(predict_one_url, "_run_pipeline", lambda *a, **k: _canned_payload(8))
    info = smoke.run_smoke(_VALID_URL, 0.0, 5.0, mock=True)

    assert info["num_timepoints"] == 8
    assert info["num_regions"] == len(REGION_IDS)
    assert info["series_lengths_uniform"] is True
    assert info["regions_match_contract"] is True
    assert smoke._shape_ok(info) is True


def test_main_ok_with_stubbed_pipeline(smoke, monkeypatch, capsys) -> None:
    import predict_one_url

    monkeypatch.setattr(predict_one_url, "_run_pipeline", lambda *a, **k: _canned_payload(8))
    rc = smoke.main([_VALID_URL, "--start-sec", "0", "--end-sec", "5", "--mock"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "smoke OK" in out
    assert "T=8 timepoints x 8 regions" in out


def test_main_rejects_non_youtube(smoke, capsys) -> None:
    rc = smoke.main(["https://www.tiktok.com/@x/video/1", "--mock"])
    assert rc == 2
    assert "invalid_url" in capsys.readouterr().err


def test_main_rejects_bad_segment(smoke, capsys) -> None:
    rc = smoke.main([_VALID_URL, "--start-sec", "30", "--end-sec", "10", "--mock"])
    assert rc == 2
    assert "bad_segment" in capsys.readouterr().err


def test_main_flags_malformed_shape(smoke, monkeypatch, capsys) -> None:
    import predict_one_url

    bad = _canned_payload(8)
    bad["byRegion"]["v1"] = [0.1] * 3  # series shorter than timestamps
    monkeypatch.setattr(predict_one_url, "_run_pipeline", lambda *a, **k: bad)

    rc = smoke.main([_VALID_URL, "--mock"])
    assert rc == 3
