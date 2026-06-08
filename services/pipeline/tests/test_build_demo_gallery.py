"""Mock-mode bake test for ``scripts/build_demo_gallery.py``.

Proves the demo-gallery tooling produces valid ActivationPayload JSON +
an index.json manifest end-to-end with MockBackend — no network, no CLI,
no GPU — and that each entry's analyzed duration is the segment span
(``endSec - startSec``, CONTRACTS.md §13.3). The bake is redirected into a
tmp dir, so it never touches the real apps/web gallery.

The script lives at the repo root, so we load it by path (it is imported
as a module, never run as ``__main__``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from neural_media_inference._shared import REGION_IDS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"


def _load_script(name: str):
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


def _assert_wire_payload(payload: dict) -> None:
    """The contract apps/web/lib/api-v2.ts enforces on a callback payload."""
    assert set(payload) == {"videoDurationSec", "timestamps", "byRegion", "modelVersion"}
    timestamps = payload["timestamps"]
    by_region = payload["byRegion"]
    assert isinstance(timestamps, list)
    assert set(by_region) == set(REGION_IDS)
    for region_id, series in by_region.items():
        assert len(series) == len(timestamps), f"{region_id}: {len(series)} != {len(timestamps)}"
    assert all(t >= 0.0 for t in timestamps)
    assert timestamps == sorted(timestamps)


def test_build_mock_only_produces_valid_gallery(gallery, tmp_path, monkeypatch) -> None:
    out = tmp_path / "demo-predictions"
    monkeypatch.setattr(gallery, "OUTPUT_DIR", out)

    rc = gallery.build(force_mock=True)
    assert rc == 0

    index = json.loads((out / "index.json").read_text())
    entries = index["entries"]
    assert len(entries) == len(gallery.DEMO_ENTRIES)

    for manifest_entry, demo in zip(entries, gallery.DEMO_ENTRIES, strict=True):
        # The manifest carries the segment and the analyzed duration.
        assert manifest_entry["startSec"] == demo.startSec
        assert manifest_entry["endSec"] == demo.endSec
        assert manifest_entry["durationSec"] == pytest.approx(demo.analyzed_duration_sec)
        assert manifest_entry["modelVersion"] == gallery.MOCK_MODEL_VERSION

        payload = json.loads((out / f"{manifest_entry['slug']}.json").read_text())
        _assert_wire_payload(payload)
        # videoDurationSec is the analyzed segment length (§13.3), not the
        # source video's full length.
        assert payload["videoDurationSec"] == pytest.approx(demo.analyzed_duration_sec)

    # Never wrote outside the tmp dir we handed it.
    assert gallery.OUTPUT_DIR == out


def test_build_mock_only_is_deterministic(gallery, tmp_path, monkeypatch) -> None:
    out_a = tmp_path / "a"
    monkeypatch.setattr(gallery, "OUTPUT_DIR", out_a)
    assert gallery.build(force_mock=True) == 0

    out_b = tmp_path / "b"
    monkeypatch.setattr(gallery, "OUTPUT_DIR", out_b)
    assert gallery.build(force_mock=True) == 0

    # Same entries + hard-coded seed → byte-identical per-entry payloads.
    for demo in gallery.DEMO_ENTRIES:
        slug = gallery.slugify(demo.label)
        assert (out_a / f"{slug}.json").read_text() == (out_b / f"{slug}.json").read_text()


# ---------------------------------------------------------------------------
# --dry-run validation gate
# ---------------------------------------------------------------------------

def test_shipped_entries_pass_dry_run(gallery, capsys) -> None:
    """The shipped DEMO_ENTRIES are real, wired clips now, so --dry-run must
    PASS (rc 0) — every shipped clip is a valid YouTube URL + an in-bounds
    segment. (Before the clips were wired this asserted the REPLACE_ME
    placeholders FAILED; that gate has served its purpose.)"""
    rc = gallery.dry_run()  # default = the shipped DEMO_ENTRIES
    out = capsys.readouterr().out
    assert rc == 0
    assert "FAIL" not in out
    n = len(gallery.DEMO_ENTRIES)
    assert f"{n}/{n} PASS" in out


def test_dry_run_passes_on_valid_entries(gallery, capsys) -> None:
    valid = (
        gallery.DemoEntry(
            label="ok-watch", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            startSec=0.0, endSec=10.0,
        ),
        gallery.DemoEntry(
            label="ok-shortlink", url="https://youtu.be/dQw4w9WgXcQ",
            startSec=5.0, endSec=20.0,
        ),
    )
    rc = gallery.dry_run(valid)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2/2 PASS" in out


def test_dry_run_flags_bad_and_oversized_segments(gallery, capsys) -> None:
    entries = (
        gallery.DemoEntry(  # start >= end -> bad_segment
            label="reversed", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            startSec=30.0, endSec=10.0,
        ),
        gallery.DemoEntry(  # > 90s -> segment_too_long
            label="too-long", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            startSec=0.0, endSec=200.0,
        ),
    )
    rc = gallery.dry_run(entries)
    out = capsys.readouterr().out
    assert rc == 1
    assert "bad_segment" in out
    assert "segment_too_long" in out
