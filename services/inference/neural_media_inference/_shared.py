"""Bridge to the repo-root `shared/` contracts package.

The inference package is `pip install -e`'d as its own distribution and does
not yet have `shared/` installed as a proper dependency. Until the
integration lead lands a real `shared` package, fall back to a sys.path
shim relative to the repo root. The api service does the same thing in
`services/api/neural_media_api/schemas_re_export.py`.
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
    InferenceRun,
    RegionMetrics,
)
