"""Smoke tests for TribeBackend.

These tests run on the mock-only path too — importing
`neural_media_inference.backend_tribe` must not require torch/tribev2.
Real-path tests (actual forward pass) are gated behind `tribev2` being
installed; they're skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest

from neural_media_inference import backend_tribe

_TRIBE_INSTALLED = importlib.util.find_spec("tribev2") is not None


def test_module_imports_without_real_extra():
    # Even when the [real] extra isn't installed, importing the module
    # MUST succeed — heavyweight deps are deferred to construction.
    assert hasattr(backend_tribe, "TribeBackend")
    assert backend_tribe.TribeBackend.model_id == "tribe-v2"


def test_lazy_reexport_from_package():
    # The package's __getattr__ trampoline must surface TribeBackend by name.
    import neural_media_inference

    assert neural_media_inference.TribeBackend is backend_tribe.TribeBackend


def test_construction_without_license_accept_rejects(tmp_path):
    with pytest.raises(PermissionError, match="accept_licenses"):
        backend_tribe.TribeBackend(videos_dir=tmp_path)


@pytest.mark.integration
@pytest.mark.skipif(not _TRIBE_INSTALLED, reason="[real] extra (tribev2) not installed")
def test_tribe_forward_smoke(tmp_path):
    """Real TRIBE v2 forward pass against a small fixture clip.

    Skipped by default — it needs both the `[real]` extra
    (`pip install '.[real]'`) and a fixture video, and realistically a GPU
    host. Point it at a clip and run on demand::

        NEURAL_MEDIA_TRIBE_FIXTURE=/path/to/clip.mp4 \\
            pytest -m integration services/inference

    Validates the wire contract the rest of the system depends on: a
    ``(T, 20484)`` float32 tensor in ``[0, 1]`` plus a populated
    reproducibility envelope (weights hash + torch version).
    """
    from neural_media_inference import TribeBackend

    fixture_src = os.environ.get("NEURAL_MEDIA_TRIBE_FIXTURE")
    if not fixture_src or not Path(fixture_src).is_file():
        pytest.skip("set NEURAL_MEDIA_TRIBE_FIXTURE=/path/to/clip.mp4 to run")
    # TribeBackend resolves {videos_dir}/{video_id}.mp4, so stage the clip there.
    shutil.copy(fixture_src, tmp_path / "vid-test.mp4")

    backend = TribeBackend(videos_dir=tmp_path, accept_licenses=True)
    out = backend.infer(
        video_id="vid-test", duration_s=2.0, seed=0, sample_rate_hz=1.5
    )
    assert out.shape[1] == 20484
    assert out.dtype.name == "float32"
    assert 0.0 <= float(out.min()) and float(out.max()) <= 1.0
    extras = backend.extra_params
    assert extras["weights_hash"]
    assert extras["torch_version"]
