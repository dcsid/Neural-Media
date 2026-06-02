"""Single local import surface for the shared contracts.

The canonical schemas live at ``/shared/schemas.py`` (repo root) so every
service speaks one wire format. The API re-exports them through this module
so route code imports from one local place instead of reaching across
service boundaries. The ``sys.path`` shim below adds the repo root so
``shared`` resolves as a plain top-level package: in this local-first
monorepo that keeps a single source of truth (no build step, no vendored
copy that could drift from the contract).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.schemas import (  # noqa: E402,F401
    NUM_VERTICES,
    REGION_DESCRIPTIONS,
    REGION_IDS,
    ActivationOutput,
    ActivationSidecar,
    AggregateBucket,
    AggregateReport,
    AuthorBucket,
    Capabilities,
    CapabilityBlocker,
    ClusterSummary,
    DebugCounts,
    DebugDiskUsage,
    DebugReport,
    ImportJob,
    ImportJobProgress,
    ImportJobStatus,
    ImportMode,
    ImportPhase,
    InferenceRun,
    RegionDef,
    RegionMetrics,
    VideoMetadata,
    WatchEvent,
)
