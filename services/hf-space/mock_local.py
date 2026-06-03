"""Local stand-in for the real HuggingFace Space — single-video pipeline.

Run:

    python services/hf-space/mock_local.py

A FastAPI app comes up on :8001 with the same `/predict` contract the real
Space (T2) is building toward. The frontend (T4) and the AWS jobs_worker
(T3) can target this server instead of the real Space during development —
no GPU, no yt-dlp, no ffmpeg, no HuggingFace account required.

Contract
========

    POST /predict
      body: {
        "jobId":      str,                       # propagated back via callback
        "source":     { "kind": "url"|"s3", "value": str },  # discriminated union
        "startSec":   float,                     # segment start (YouTube url path; §13.5)
        "endSec":     float,                     # segment end (ignored for kind=="s3")
        "callbackUrl": str,                      # AWS API Gateway URL for the callback
        "callbackToken": str,                    # forwarded as X-NM-Token header
      }
      response: 202 { "jobId": str, "accepted": true }

After a randomised 5-15s "inference" delay, the server POSTs to
`callbackUrl` with the same schema the real Space (T2) uses — the
activation JSON is gzipped then base64-encoded into `activationsB64`
so the AWS hf_callback Lambda's decode path runs in dev too:

    POST {callbackUrl}
      headers: X-NM-Token: {callbackToken}
      body: {
        "jobId": str,
        "status": "done" | "failed_download" | "failed_inference" | "rejected_duration",
        "activationsB64": str | null,  # base64(gzip(JSON.stringify(result))); required iff status=="done"
        "durationSec": float | null,
        "modelVersion": str | null,
        "error": str | null,
      }

Failure mode: 1 in 8 requests returns `status="failed_download"` with
`error="download_blocked"`, so the frontend's error path can be exercised
without ever hitting a real download.

Why not Docker
==============

The real HF Space ships with a Dockerfile because TRIBE + ffmpeg + yt-dlp
won't fit anywhere else. The mock has no such dependencies — pure Python
plus FastAPI / uvicorn / httpx, all of which the inference and api services
already require. `python services/hf-space/mock_local.py` is the entire
boot recipe.

Why a callback (and not a sync response)
========================================

T3's AWS template invokes the Space asynchronously (Lambda → HTTPS, fire-
and-forget) because real inference takes 30-60 seconds — longer than API
Gateway will hold a connection open. The callback is the only way the
results get back to DynamoDB / S3. The mock mirrors this so the same
jobs_worker code path runs end-to-end against the local mock.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Literal, Union

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Reuse the existing seed-based mock so the byRegion timeseries the
# frontend sees here are the same shape the real backend will emit.
# Adding services/inference to sys.path lets us run this file directly
# (`python services/hf-space/mock_local.py`) without installing the
# `neural_media_inference` package — handy for the dev-single loop where
# the user hasn't run `make install` yet.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "services" / "inference"))

from neural_media_inference.aggregate import _region_timeseries  # noqa: E402
from neural_media_inference.backend import MockBackend  # noqa: E402

REGION_IDS = ("v1", "v2", "v3", "v4", "auditory", "language", "ffa", "vwfa")
NUM_SAMPLES = 24                # T — matches the brief's contract
DURATION_MIN_SEC = 15.0
DURATION_MAX_SEC = 45.0
INFER_DELAY_MIN = 5.0
INFER_DELAY_MAX = 15.0
FAILURE_RATE = 1 / 8            # 1-in-8 simulated `failed_download`

log = logging.getLogger("hf-mock")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")


class SourceUrl(BaseModel):
    kind: Literal["url"]
    value: str


class SourceS3(BaseModel):
    kind: Literal["s3"]
    value: str


class PredictRequest(BaseModel):
    jobId: str = Field(..., description="Caller-supplied job id, echoed back")
    source: Union[SourceUrl, SourceS3] = Field(
        ...,
        description="Where to fetch the video from — discriminated by `kind`",
        discriminator="kind",
    )
    callbackUrl: str = Field(
        ..., description="Where to POST the result when inference finishes"
    )
    callbackToken: str = Field(
        ...,
        min_length=1,
        description="Sent back as X-NM-Token so the AWS hf_callback Lambda can authenticate",
    )
    # Segment window (§13.5). The mock ignores it — it always synthesises a
    # full stub — but accepts the fields so a segment-bearing request from the
    # frontend / worker doesn't look malformed in the dev loop.
    startSec: float | None = None
    endSec: float | None = None


app = FastAPI(title="mock-hf-space", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "mock-hf-space"}


@app.post("/predict", status_code=202)
async def predict(req: PredictRequest) -> dict[str, object]:
    """Schedule a deferred callback and return 202 immediately.

    The real Space will also return 202 — TRIBE inference is too slow to
    hold an HTTP connection open. We `asyncio.create_task` instead of
    blocking so the request handler returns promptly.
    """
    if not req.callbackUrl.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail=f"callbackUrl must be http(s), got: {req.callbackUrl!r}",
        )
    asyncio.create_task(_run_and_callback(req))
    log.info("accept jobId=%s source.kind=%s", req.jobId, req.source.kind)
    return {"jobId": req.jobId, "accepted": True}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _run_and_callback(req: PredictRequest) -> None:
    delay = random.uniform(INFER_DELAY_MIN, INFER_DELAY_MAX)
    log.info("jobId=%s sleeping %.1fs", req.jobId, delay)
    await asyncio.sleep(delay)

    if random.random() < FAILURE_RATE:
        payload: dict[str, object] = {
            "jobId": req.jobId,
            "status": "failed_download",
            "error": "download_blocked",
        }
        log.info("jobId=%s simulated failure: download_blocked", req.jobId)
    else:
        duration = random.uniform(DURATION_MIN_SEC, DURATION_MAX_SEC)
        result = _stub_activations(req.jobId, duration)
        # The real Space ships activations as base64(gzip(JSON)) so the
        # hf_callback Lambda can stream the bytes straight into S3 with
        # ContentEncoding: gzip. Mirror that here so sam-local dev exercises
        # the same decode path.
        activations_b64 = base64.b64encode(
            gzip.compress(json.dumps(result).encode("utf-8"))
        ).decode("ascii")
        payload = {
            "jobId": req.jobId,
            "status": "done",
            "activationsB64": activations_b64,
            "durationSec": duration,
            "modelVersion": str(result.get("modelVersion") or "mock-0.1.0"),
        }
        log.info(
            "jobId=%s done duration=%.1fs T=%d b64len=%d",
            req.jobId,
            duration,
            len(result["timestamps"]),
            len(activations_b64),
        )

    headers = {"X-NM-Token": req.callbackToken}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(req.callbackUrl, json=payload, headers=headers)
        log.info(
            "jobId=%s callback %s -> %d", req.jobId, req.callbackUrl, r.status_code
        )
    except httpx.HTTPError as exc:  # noqa: BLE001 — log + drop; mock is fire-and-forget
        log.warning("jobId=%s callback failed: %s", req.jobId, exc)


def _stub_activations(job_id: str, duration_sec: float) -> dict[str, object]:
    """Run MockBackend at low resolution, aggregate to per-region timeseries.

    `MockBackend.infer` produces (T, 20484) fp32 in [0, 1]; we average each
    region's vertex columns down to a single scalar per timepoint using the
    same aggregator the real pipeline calls. Result shape: 8 × NUM_SAMPLES
    fp32 lists, JSON-serialisable.

    Why ``sample_rate_hz = NUM_SAMPLES / duration`` instead of a fixed Hz:
    the contract pins T (24), not the rate. Computing the rate from the
    duration keeps timestamps[0]==0 and timestamps[-1]≈duration so the
    frontend scrubber's domain lines up with the video.
    """
    sample_rate_hz = NUM_SAMPLES / duration_sec
    seed = abs(hash(job_id)) % (2**31)
    backend = MockBackend()
    activations = backend.infer(
        video_id=job_id,
        duration_s=duration_sec,
        seed=seed,
        sample_rate_hz=sample_rate_hz,
    )
    # MockBackend rounds the sample count via `round(duration * rate)`;
    # nudge to exactly NUM_SAMPLES so the frontend's fixed-T renderer
    # doesn't have to special-case off-by-ones.
    if activations.shape[0] != NUM_SAMPLES:
        idx = np.linspace(0, activations.shape[0] - 1, NUM_SAMPLES).round().astype(int)
        activations = activations[idx]

    timestamps = np.linspace(0.0, duration_sec, NUM_SAMPLES, dtype=np.float32).tolist()
    by_region: dict[str, list[float]] = {}
    for region in REGION_IDS:
        ts = _region_timeseries(activations, region).astype(np.float32)
        by_region[region] = [float(v) for v in ts]

    return {
        "videoDurationSec": float(duration_sec),
        "timestamps": [float(v) for v in timestamps],
        "byRegion": by_region,
        "modelVersion": f"{MockBackend.model_id}@{MockBackend.model_version}",
    }


def main() -> None:
    port = int(os.environ.get("MOCK_HF_PORT", "8001"))
    log.info("starting mock HF Space on :%d", port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
