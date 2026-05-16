"""Catalog store — STUB.

Owner: api-orchestrator worker. See docs/worker-briefs/api-orchestrator.md.

Two implementations expected, behind one interface:
  - `SampleStore`: read pre-generated outputs from
    `data/sample/mock_inference/` so the vertical slice runs with no GPU.
  - `SqliteStore`: SQLite-backed persistence for `videos`, `watch_events`,
    `inference_runs`. Parquet/NPZ activations on disk.

Both must expose the same read methods used by `main.py` route handlers
(see CONTRACTS.md §7).
"""

from __future__ import annotations

# TODO(api-orchestrator): implement SampleStore + SqliteStore.
