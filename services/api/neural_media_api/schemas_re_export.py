"""Re-export of shared contracts so routes don't import across services.

The canonical shared module lives at `/shared/schemas.py` (repo-root). For
the scaffold we import from a sibling path. The api-orchestrator worker
should replace this with a proper `pip install -e ./shared` package layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED.parent))

from shared.schemas import (  # noqa: E402,F401
    NUM_VERTICES,
    REGION_DESCRIPTIONS,
    REGION_IDS,
    ActivationOutput,
    ActivationSidecar,
    AggregateBucket,
    AggregateReport,
    ClusterSummary,
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
