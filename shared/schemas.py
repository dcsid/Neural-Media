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


class WatchEvent(BaseModel):
    id: str
    video_id: str
    watched_at: datetime
    duration_watched_s: float | None = None
    completion_pct: float | None = None
    source: Literal["tiktok_export"] = "tiktok_export"


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


class RegionDef(BaseModel):
    region_id: str
    description: str


class AggregateBucket(BaseModel):
    mean: float
    peak: float


class ClusterSummary(BaseModel):
    cluster_id: int
    size: int
    exemplar_video_ids: list[str]


class AggregateReport(BaseModel):
    total_videos: int
    total_watch_time_s: float
    first_watched_at: datetime | None
    last_watched_at: datetime | None
    by_region: dict[str, AggregateBucket]
    by_hour_of_day: list[float]
    by_day_of_week: list[float]
    clusters: list[ClusterSummary]


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


# ---------------------------------------------------------------------------
# Import job (CONTRACTS.md §8)
# ---------------------------------------------------------------------------

ImportJobStatus = Literal["queued", "running", "complete", "partial", "failed"]
ImportMode = Literal["mock", "real"]
ImportPhase = Literal["parsing", "downloading", "preprocessing", "inferring"]


class ImportJobProgress(BaseModel):
    """Nested progress block on `ImportJob`. `total` is null until the
    parsing phase completes; `phase` is null until the first progress
    event arrives.
    """

    current: int = 0
    total: int | None = None
    phase: str | None = None


class ImportJob(BaseModel):
    """Polling response from `GET /api/v1/import/{job_id}`. Also returned
    by `POST /api/v1/import` on success and on the 409 single-job-gate
    response (where it carries the running job).
    """

    id: str
    status: ImportJobStatus
    mode: ImportMode
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    progress: ImportJobProgress = Field(default_factory=ImportJobProgress)
    error: str | None = None
    source_filename: str | None = None
