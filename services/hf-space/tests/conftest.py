"""Make `app` importable when pytest runs from services/hf-space.

Adds the Space dir (for `import app` / `import mock_local`) and
services/inference (for the `neural_media_inference` package app.py imports at
module load) to sys.path. The tests cover pure validation + yt-dlp command
construction — no GPU, network, yt-dlp, or ffmpeg is exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPACE_DIR = _HERE.parent                       # services/hf-space
_REPO_ROOT = _SPACE_DIR.parent.parent           # repo root
for _p in (_SPACE_DIR, _REPO_ROOT / "services" / "inference"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
