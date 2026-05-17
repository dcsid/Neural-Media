"""Neural Media inference package.

Owned by the ml-inference worker. The public surface below is imported
verbatim by api-orchestrator (`docs/worker-briefs/ml-inference.md`):

    from neural_media_inference import (
        run_inference,
        MockBackend,
        REGION_VERTEX_MASKS,
        aggregate_region_metrics,
    )

`TribeBackend` (real TRIBE v2) is also importable from this namespace,
but only construction triggers the heavyweight torch/tribev2 imports — so
mock-only installs aren't burdened with multi-GB transitive deps.

Do not remove or rename these names without a coordinated cross-team change.
"""

from .aggregate import (
    REGION_VERTEX_MASKS,
    aggregate_region_metrics,
    downsample_region_means,
    keyframe_vertex_snapshots,
)
from .backend import InferenceBackend, MockBackend
from .runner import RunArtifacts, run_inference

__version__ = "0.0.2"

__all__ = [
    "InferenceBackend",
    "MockBackend",
    "REGION_VERTEX_MASKS",
    "RunArtifacts",
    "TribeBackend",
    "aggregate_region_metrics",
    "downsample_region_means",
    "keyframe_vertex_snapshots",
    "run_inference",
]


def __getattr__(name: str):
    # Lazy: only pulls in torch / tribev2 when someone actually asks.
    if name == "TribeBackend":
        from .backend_tribe import TribeBackend
        return TribeBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
