"""Wire-invariant tests for the two repo-root predictor scripts.

Both `scripts/predict_one_url.py` and `scripts/build_demo_gallery.py` emit the
camelCase activation payload the frontend consumes, and
`apps/web/lib/api-v2.ts` hard-rejects any payload where a region series length
differs from `timestamps`. These tests pin that invariant end-to-end against
the deterministic MockBackend (no network, no GPU, no torch), plus the T=1
edge case, so a regression in either script's timestamp/region wiring fails
here instead of in the browser.

The scripts live at the repo root, not in this package, so we load them by
path. They are imported as modules (never run as `__main__`), and nothing is
written outside the test's tmp_path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from neural_media_inference._shared import REGION_IDS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_script(name: str):
    """Import a repo-root script as a module.

    Registers it in sys.modules BEFORE exec because both scripts define
    module-level dataclasses, and on Python 3.12+ `@dataclass` looks the
    defining module up in sys.modules while the class body runs.
    """
    path = _SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gallery():
    return _load_script("build_demo_gallery")


@pytest.fixture(scope="module")
def predict():
    return _load_script("predict_one_url")


def _assert_wire_payload(payload: dict) -> None:
    """The contract apps/web/lib/api-v2.ts enforces on a callback payload."""
    assert set(payload) == {"videoDurationSec", "timestamps", "byRegion", "modelVersion"}
    timestamps = payload["timestamps"]
    by_region = payload["byRegion"]
    assert isinstance(timestamps, list)
    assert set(by_region) == set(REGION_IDS)
    for region_id, series in by_region.items():
        assert len(series) == len(timestamps), (
            f"{region_id}: series len {len(series)} != timestamps len {len(timestamps)}"
        )
    # Time axis is non-negative and monotonic non-decreasing.
    assert all(t >= 0.0 for t in timestamps)
    assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# build_demo_gallery.py — _mock_predict
# ---------------------------------------------------------------------------

def test_gallery_mock_predict_wire_invariants(gallery, tmp_path):
    entry = gallery.DemoEntry(label="Test clip", url="https://x/1", duration_sec=18.0)
    out = tmp_path / "clip.json"
    payload = gallery._mock_predict(entry, out)

    _assert_wire_payload(payload)
    assert payload["videoDurationSec"] == 18.0
    assert payload["modelVersion"] == gallery.MOCK_MODEL_VERSION
    # Writes only to the path we hand it — never the real gallery dir.
    assert out.exists()
    assert gallery.OUTPUT_DIR not in out.parents


def test_gallery_mock_predict_t1_edge(gallery, tmp_path):
    # A sub-second clip downsamples to a single timepoint; the script special-
    # cases this to timestamps == [0.0] so the length invariant still holds.
    entry = gallery.DemoEntry(label="Blink", url="https://x/2", duration_sec=0.25)
    payload = gallery._mock_predict(entry, tmp_path / "blink.json")

    assert payload["timestamps"] == [0.0]
    _assert_wire_payload(payload)
    assert all(len(series) == 1 for series in payload["byRegion"].values())


def test_gallery_mock_predict_is_deterministic(gallery, tmp_path):
    entry = gallery.DemoEntry(label="Same", url="https://x/3", duration_sec=20.0)
    a = gallery._mock_predict(entry, tmp_path / "a.json")
    b = gallery._mock_predict(entry, tmp_path / "b.json")
    assert a == b


# ---------------------------------------------------------------------------
# predict_one_url.py — _run_pipeline (download/preprocess stubbed out)
# ---------------------------------------------------------------------------

def _stub_pipeline_io(predict, monkeypatch, tmp_path, *, duration_s: float):
    """Replace the network/ffmpeg/preprocess steps so _run_pipeline exercises
    only the MockBackend → aggregate → wire-payload path."""
    raw = tmp_path / "raw.mp4"
    raw.write_bytes(b"\x00")
    proc = tmp_path / "proc.mp4"
    proc.write_bytes(b"\x00")

    monkeypatch.setattr(
        predict, "download_video",
        lambda *a, **k: SimpleNamespace(ok=True, local_path=raw, error=None),
    )
    monkeypatch.setattr(predict, "_ffprobe_duration", lambda _p: duration_s)
    monkeypatch.setattr(
        predict, "preprocess_video",
        lambda *a, **k: SimpleNamespace(local_path=proc, skipped=False),
    )


def test_predict_one_url_pipeline_wire_invariants(predict, monkeypatch, tmp_path):
    _stub_pipeline_io(predict, monkeypatch, tmp_path, duration_s=24.0)
    payload = predict._run_pipeline(
        "https://www.youtube.com/watch?v=aaaaaaaaaaa", mock=True, max_duration_sec=None
    )

    _assert_wire_payload(payload)
    assert payload["videoDurationSec"] == 24.0
    # Mock backend reports the mock model version, not a real weights hash.
    assert payload["modelVersion"] == predict.MockBackend.model_version


def test_predict_one_url_pipeline_t1_edge(predict, monkeypatch, tmp_path):
    # 0.3 s at 1.5 Hz → a single timepoint; timestamps and every region
    # series must still be length 1.
    _stub_pipeline_io(predict, monkeypatch, tmp_path, duration_s=0.3)
    payload = predict._run_pipeline(
        "https://www.youtube.com/watch?v=bbbbbbbbbbb", mock=True, max_duration_sec=None
    )

    assert len(payload["timestamps"]) == 1
    _assert_wire_payload(payload)
    assert all(len(series) == 1 for series in payload["byRegion"].values())
