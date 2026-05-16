"""Make the inference package importable when pytest is invoked from
`services/inference/`. Mirrors the sys.path shim in the runner so tests
don't depend on the package being pip-installed."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_DIR = _HERE.parent  # services/inference
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
