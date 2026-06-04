"""Pydantic schemas — canonical Python contracts for Neural Media.

Mirror of shared/types.ts and shared/CONTRACTS.md. Any change to one MUST be
reflected in the other two in the same PR.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Canonical region set. Order is the API contract — do not shuffle.
# ---------------------------------------------------------------------------

REGION_IDS: tuple[str, ...] = (
    "v1",
    "v2",
    "v3",
    "v4",
    "auditory",
    "language",
    "ffa",
    "vwfa",
)

REGION_DESCRIPTIONS: dict[str, str] = {
    "v1": "Primary visual cortex",
    "v2": "Secondary visual cortex",
    "v3": "Tertiary visual cortex",
    "v4": "V4 (color/form)",
    "auditory": "Primary + belt auditory cortex",
    "language": "Lateral language network",
    "ffa": "Fusiform face area",
    "vwfa": "Visual word form area",
}

NUM_VERTICES: int = 20_484


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------

class VideoMetadata(BaseModel):
    id: str
    source_url: str
    title: str | None = None
    author: str | None = None
    duration_s: float = 0.0
    downloaded: bool = False
    local_path: str | None = None
    tags: list[str] = Field(default_factory=list)


InferenceStatus = Literal["pending", "running", "complete", "failed"]


class InferenceRun(BaseModel):
    id: str
    video_id: str
    model_id: str
    model_version: str
    seed: int
    params_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    activation_path: str
    status: InferenceStatus = "pending"


class RegionMetrics(BaseModel):
    region_id: str
    video_id: str
    inference_run_id: str
    mean: float
    peak: float
    sustained: float
    timeseries: list[float]


# ---------------------------------------------------------------------------
# Activation envelope: in-memory + sidecar shapes
# ---------------------------------------------------------------------------

class ActivationSidecar(BaseModel):
    """The `{inference_run_id}.meta.json` file alongside the activation Parquet."""

    inference_run_id: str
    video_id: str
    num_vertices: int = NUM_VERTICES
    num_timepoints: int
    sample_rate_hz: float
    model_id: str
    seed: int


class ActivationOutput(BaseModel):
    """Wire-format (downsampled) activation payload served by the API.

    Frontend MUST NOT request raw 20484-dim arrays — the server will
    return region-level means + a per-vertex snapshot only at a few keyframes.
    """

    inference_run_id: str
    video_id: str
    num_vertices: int = NUM_VERTICES
    num_timepoints: int
    sample_rate_hz: float
    timestamps: list[float]
    keyframe_vertices: dict[str, list[float]]
    region_means: dict[str, list[float]]
