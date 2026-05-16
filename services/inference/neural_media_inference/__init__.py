"""Neural Media inference package.

Owned by the ml-inference worker. The public surface below is imported
verbatim by api-orchestrator (`docs/worker-briefs/ml-inference.md`):

    from neural_media_inference import (
        run_inference,
        MockBackend,
        REGION_VERTEX_MASKS,
        aggregate_region_metrics,
    )

Do not remove or rename these without a coordinated cross-team change.
"""

from .aggregate import (
    REGION_VERTEX_MASKS,
    aggregate_region_metrics,
    downsample_region_means,
    keyframe_vertex_snapshots,
)
from .backend import InferenceBackend, MockBackend
from .runner import RunArtifacts, run_inference

__version__ = "0.0.1"

__all__ = [
    "InferenceBackend",
    "MockBackend",
    "REGION_VERTEX_MASKS",
    "RunArtifacts",
    "aggregate_region_metrics",
    "downsample_region_means",
    "keyframe_vertex_snapshots",
    "run_inference",
]
