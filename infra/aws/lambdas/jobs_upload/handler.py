"""Direct-to-S3 uploads for MP4s the user wants to analyze.

Two routes share one Lambda:
  POST /v2/jobs/upload                       — mint a presigned PUT
  POST /v2/jobs/upload/{jobId}/confirm       — confirm the upload landed
                                               and kick the worker

We mint the job row at upload-creation time (status=pending,
uploadConfirmed=false) so the user always has a jobId to poll, even if
the PUT itself fails. The 1-day lifecycle rule on `uploads/` evaporates
abandoned objects for free.
"""
from __future__ import annotations

import math
import os
from decimal import Decimal

from shared import (
    STATUS_PENDING,
    async_invoke_worker,
    default_expires_at,
    get_job,
    json_response,
    new_job_id,
    now_epoch,
    parse_body,
    presigned_put,
    put_job,
    update_job,
)

WORKER_FUNCTION_NAME = os.environ["WORKER_FUNCTION_NAME"]
UPLOAD_TTL_SEC = 900  # 15 minutes — long enough for ~100 MB on coffee-shop wifi

# Hard ceiling on the analysis window (CONTRACTS.md §13.2) — same cap as
# jobs_create; uploads can now carry a [start, end) segment the Space trims to.
_MAX_SEGMENT_SEC = 90.0


def _is_finite_number(x: object) -> bool:
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
    )


def _segment_error(start: object, end: object) -> str | None:
    """error_code for an invalid [start, end) segment, else None (mirrors
    jobs_create._segment_error / CONTRACTS §13.2)."""
    if not _is_finite_number(start) or not _is_finite_number(end):
        return "bad_segment"
    if start < 0 or start >= end:
        return "bad_segment"
    if end - start > _MAX_SEGMENT_SEC:
        return "segment_too_long"
    return None


def _create_upload(_event: dict) -> dict:
    job_id = new_job_id()
    key = f"uploads/{job_id}.mp4"
    upload_url = presigned_put(
        key, expires_in=UPLOAD_TTL_SEC, content_type="video/mp4"
    )
    now = now_epoch()
    put_job(
        {
            "jobId": job_id,
            "status": STATUS_PENDING,
            "source": "s3",
            "uploadKey": key,
            "uploadConfirmed": False,
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": default_expires_at(),
        }
    )
    return json_response(
        201,
        {
            "jobId": job_id,
            "uploadUrl": upload_url,
            "uploadKey": key,
            "uploadExpiresInSec": UPLOAD_TTL_SEC,
        },
    )


def _confirm_upload(event: dict) -> dict:
    job_id = (event.get("pathParameters") or {}).get("jobId")
    if not job_id:
        return json_response(400, {"error": "missing_job_id"})
    job = get_job(job_id)
    if not job:
        return json_response(404, {"error": "job_not_found"})
    if job.get("source") != "s3":
        return json_response(409, {"error": "not_an_upload_job"})

    # Optional [startSec, endSec) segment of the uploaded file (CONTRACTS §13.4):
    # the Space trims to it; absent → whole file. Same validation as the URL
    # path. Stored as Decimal so jobs_worker forwards it to the Space /predict.
    body = parse_body(event)
    start_sec, end_sec = body.get("startSec"), body.get("endSec")
    fields: dict = {"uploadConfirmed": True, "updatedAt": now_epoch()}
    if start_sec is not None or end_sec is not None:
        seg_err = _segment_error(start_sec, end_sec)
        if seg_err is not None:
            return json_response(400, {"error_code": seg_err})
        fields["startSec"] = Decimal(str(start_sec))
        fields["endSec"] = Decimal(str(end_sec))

    update_job(job_id, **fields)
    async_invoke_worker(job_id, WORKER_FUNCTION_NAME)
    return json_response(200, {"jobId": job_id, "status": STATUS_PENDING})


def lambda_handler(event: dict, _context) -> dict:
    # Prefer routeKey when API Gateway provides it (it always does for
    # HTTP API v2); fall back to rawPath suffix for tooling that omits it
    # (local sam invoke with hand-written events, etc.).
    route = event.get("routeKey") or ""
    if route == "POST /v2/jobs/upload":
        return _create_upload(event)
    if route == "POST /v2/jobs/upload/{jobId}/confirm":
        return _confirm_upload(event)
    raw = event.get("rawPath") or ""
    if raw.endswith("/confirm"):
        return _confirm_upload(event)
    return _create_upload(event)
