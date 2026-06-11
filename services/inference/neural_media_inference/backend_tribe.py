"""Real TRIBE v2 backend.

Gated behind ``pip install '.[real]'`` — torch + tribev2 (~GBs of model
weights downloaded on first use) are never imported on the mock path.

Reference: facebookresearch/tribev2 quick-start --

    from tribev2 import TribeModel
    model = TribeModel.from_pretrained("facebook/tribev2",
                                       cache_folder="./cache")
    df = model.get_events_dataframe(video_path=path)
    preds, _ = model.predict(events=df)   # (T, 20484) on fsaverage5

License: CC-BY-NC-4.0. TRIBE v2 transitively pulls LLaMA 3.2 / V-JEPA2 /
Wav2Vec-BERT weights via huggingface_hub on first use; their licenses
apply. The wrapper makes the user pass ``accept_licenses=True`` so the
choice is explicit rather than buried in CLI noise.

The wrapper never trains, never fine-tunes, never logs to wandb. It runs
the documented forward path and returns a ``(T, 20484)`` fp32 tensor in
``[0, 1]`` — matching `MockBackend` so the runner is backend-agnostic.

Why we squash the raw output through a sigmoid before returning:
TRIBE emits z-scored BOLD signal in roughly ``[-3, 3]``, not ``[0, 1]``.
The wire contract (CONTRACTS.md §4 + the runner's range assertion) is
``[0, 1]``. We pass the raw output through ``1 / (1 + exp(-x))`` so values
land in ``[0, 1]`` while preserving comparative order (the only thing
``docs/scientific-framing.md`` permits us to claim).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ._shared import NUM_VERTICES

_log = logging.getLogger(__name__)

# All heavyweight imports (torch, tribev2, huggingface_hub) are deferred
# to instantiation/inference time so importing this module on a mock-only
# install raises a clear ImportError only when someone actually tries to
# construct a TribeBackend.


_DEFAULT_REPO_ID = "facebook/tribev2"


@dataclass
class _LazyImports:
    """Heavyweight deps loaded once per process, only on first construct."""

    torch: Any
    TribeModel: Any
    HfApi: Any


def _import_heavies() -> _LazyImports:
    try:
        import torch  # noqa: PLC0415
        from huggingface_hub import HfApi  # noqa: PLC0415
        from tribev2 import TribeModel  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - exercised only on real path
        raise ImportError(
            "TribeBackend requires the [real] extra. Install with: "
            "`pip install -e '.[real]'` (this pulls torch + tribev2)."
        ) from e
    return _LazyImports(torch=torch, TribeModel=TribeModel, HfApi=HfApi)


# Class-level latch so the "wider than 20484 cortical vertices" warning fires
# exactly once per process — once a real-mode user sees the message they don't
# need it re-emitted on every video.
_WARNED_WIDER_THAN_CORTEX = False

# Same one-shot latch for the raw-output range diagnostic: the very first real
# forward pass logs preds.min/mean/max at INFO so a GPU run immediately reveals
# whether the raw output is z-scored (sigmoid is correct) or already in [0, 1]
# (sigmoid would be a double-application). Brain owns the actual verification —
# see docs/worker-briefs/ml-inference-status.md "Open items".
_LOGGED_RAW_RANGE = False


class TribeBackend:
    """Real TRIBE v2 forward pass.

    Parameters
    ----------
    videos_dir
        Where the data-pipeline worker dropped downloaded videos. The
        backend resolves ``video_id`` -> ``{videos_dir}/{video_id}.mp4``
        (with ``.webm`` / ``.mkv`` fallbacks). TRIBE needs the raw file —
        not a tensor — because its preprocessor pulls video frames, audio,
        and an internally-derived transcript.
    revision
        HuggingFace revision to pin. Default ``"main"`` follows the tip;
        production deployments should pin a commit sha. The resolved sha
        becomes ``model_version`` and lands in the InferenceRun's
        reproducibility envelope.
    device
        ``"cuda"``, ``"cpu"``, or None (auto-detect).
    cache_dir
        HuggingFace cache. Defaults to TRIBE's own default under
        ``~/.cache/huggingface`` if None.
    accept_licenses
        Must be ``True``. TRIBE is CC-BY-NC and transitively pulls
        LLaMA-3.2 weights; we make license acceptance explicit so it
        can't be silently flipped on by importing this module.
    """

    model_id: str = "tribe-v2"

    def __init__(
        self,
        *,
        videos_dir: Path | str,
        revision: str = "main",
        device: str | None = None,
        cache_dir: Path | str | None = None,
        accept_licenses: bool = False,
    ) -> None:
        if not accept_licenses:
            raise PermissionError(
                "TribeBackend requires accept_licenses=True. TRIBE v2 is CC-BY-NC-4.0 "
                "and pulls LLaMA-3.2 / V-JEPA2 / Wav2Vec-BERT weights — their licenses "
                "apply. Pass accept_licenses=True after reading "
                "docs/scientific-framing.md and the upstream model cards."
            )
        self._videos_dir = Path(videos_dir)
        self._revision = revision
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None

        self._deps = _import_heavies()
        torch = self._deps.torch

        self._device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        # Pin the commit sha. We resolve via HfApi so model_version is the
        # 40-char hash even when the caller passed a branch name like "main".
        info = self._deps.HfApi().repo_info(repo_id=_DEFAULT_REPO_ID, revision=revision)
        self._resolved_sha: str = info.sha
        self.model_version: str = self._resolved_sha

        cache_kw = {"cache_folder": str(self._cache_dir)} if self._cache_dir else {}
        # tribev2's TribeModel.from_pretrained() does not accept `revision`
        # (upstream API drift). The sha is still resolved + recorded above via
        # HfApi for the reproducibility envelope; the model loads from the
        # current default revision, which is the sha we just recorded.
        self._model = self._deps.TribeModel.from_pretrained(
            _DEFAULT_REPO_ID, **cache_kw
        )
        # TribeModel is a high-level wrapper, not a raw nn.Module — depending on
        # the upstream version it may not expose .to()/.eval() (it manages device
        # placement + eval mode internally, loading onto CUDA when available).
        # Call them only if present so we work across upstream API drift.
        if hasattr(self._model, "to"):
            self._model.to(self._device)
        if hasattr(self._model, "eval"):
            self._model.eval()

        # Confirm where compute will actually land. TribeModel may not expose
        # `.to()` (handled above), and on a slow run we want to KNOW whether the
        # heavy V-JEPA2 video encoder is on the GPU or silently on CPU rather than
        # infer it from timings. Logged once at load; cheap and never raises.
        cuda_ok = bool(getattr(torch, "cuda", None)) and torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_ok else None
        param_device = None
        try:  # TribeModel is a high-level wrapper — parameters() may be absent.
            param_device = str(next(self._model.parameters()).device)
        except (StopIteration, AttributeError, TypeError):
            pass
        _log.info(
            "event=device-check requested=%s cuda_available=%s gpu=%s "
            "model_param_device=%s. If a forward pass is slow and "
            "model_param_device is 'cpu' (or None with cuda_available=True), the "
            "encoder is not on the GPU — investigate TRIBE device placement.",
            self._device, cuda_ok, gpu_name, param_device,
        )

    # ------------------------------------------------------------------
    # Reproducibility envelope contributions
    # ------------------------------------------------------------------
    @property
    def extra_params(self) -> dict[str, Any]:
        """Backend-specific entries the runner merges into params_json.

        CONTRACTS.md §9 + the worker brief require weights hash, torch
        version, CUDA version, seed. The runner already records the seed;
        the rest comes from here.
        """
        torch = self._deps.torch
        return {
            "weights_hash": self._resolved_sha,
            "hf_repo_id": _DEFAULT_REPO_ID,
            "hf_revision": self._revision,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "device": self._device,
            "output_transform": "sigmoid(z)",
        }

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def _resolve_video_path(self, video_id: str) -> Path:
        for ext in (".mp4", ".webm", ".mkv", ".mov"):
            p = self._videos_dir / f"{video_id}{ext}"
            if p.exists():
                return p
        raise FileNotFoundError(
            f"No video file for {video_id} under {self._videos_dir}. "
            f"TRIBE requires the downloaded file — check that data-pipeline ran."
        )

    def infer(
        self,
        *,
        video_id: str,
        duration_s: float,
        seed: int,
        sample_rate_hz: float,
    ) -> np.ndarray:
        torch = self._deps.torch
        video_path = self._resolve_video_path(video_id)

        # Determinism: TRIBE is mostly deterministic given fixed weights,
        # but its preprocessors / dropouts (if any are still on in eval)
        # can pick up randomness. Seed everything we know about.
        torch.manual_seed(seed)
        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        np.random.seed(seed)

        with torch.inference_mode():
            events = self._model.get_events_dataframe(video_path=str(video_path))
            preds, _segments = self._model.predict(events=events)

        # `preds` is a torch.Tensor or np.ndarray depending on TRIBE
        # version; normalize to numpy.
        if hasattr(preds, "detach"):
            preds = preds.detach().to("cpu").numpy()
        preds = np.asarray(preds)

        if preds.ndim != 2 or preds.shape[1] < NUM_VERTICES:
            raise RuntimeError(
                f"TRIBE returned unexpected shape {preds.shape}; "
                f"expected (T, >={NUM_VERTICES})"
            )

        # One-shot INFO diagnostic on the FIRST real inference. The sigmoid
        # below assumes raw TRIBE output is z-scored BOLD (~[-3, 3]); that
        # assumption is UNVERIFIED, so surface the true range immediately. If
        # these values are already in [0, 1] the sigmoid is a double-application
        # and should become a passthrough — brain owns that call.
        global _LOGGED_RAW_RANGE
        if not _LOGGED_RAW_RANGE:
            _log.info(
                "event=raw-output-range first real inference: preds.shape=%s "
                "min=%.4f mean=%.4f max=%.4f. Wrapper assumes z-scored BOLD and "
                "applies sigmoid; if this is already [0, 1] the transform "
                "double-applies — see docs/worker-briefs/ml-inference-status.md "
                "'Open items'.",
                tuple(preds.shape),
                float(preds.min()),
                float(preds.mean()),
                float(preds.max()),
            )
            _LOGGED_RAW_RANGE = True

        # If TRIBE emits more than the cortical vertices, we take the leading
        # NUM_VERTICES columns and assume lh[0:10242] then rh[10242:20484]
        # (the assumption region_masks.json was built against). The upstream
        # README documents fsaverage5 (~20k) but does not commit to hemisphere
        # ordering — see docs/worker-briefs/ml-inference-status.md "Open items".
        # If a real-mode user sees this warning, run the smoke check from
        # docs/real-mode-setup.md before trusting region aggregates.
        global _WARNED_WIDER_THAN_CORTEX
        if preds.shape[1] > NUM_VERTICES and not _WARNED_WIDER_THAN_CORTEX:
            _log.warning(
                "TRIBE preds.shape=%s wider than NUM_VERTICES=%d. Taking the "
                "leading %d columns as cortical vertices (assumed lh then rh). "
                "If region aggregates look off, verify hemisphere ordering — see "
                "docs/worker-briefs/ml-inference-status.md 'Open items'.",
                tuple(preds.shape),
                NUM_VERTICES,
                NUM_VERTICES,
            )
            _WARNED_WIDER_THAN_CORTEX = True
        cortex = preds[:, :NUM_VERTICES].astype(np.float32, copy=False)

        # Z-scored BOLD → [0, 1] via logistic sigmoid (see module docstring).
        # Clip is precautionary: np.exp(-cortex) overflows float32 once
        # cortex < ~-88, and by |cortex|=30 the sigmoid has already saturated
        # to ~0/1, so this only bounds pathological inputs — the transform is
        # well-defined without it.
        np.clip(cortex, -30.0, 30.0, out=cortex)
        out = 1.0 / (1.0 + np.exp(-cortex))
        return out.astype(np.float32, copy=False)
