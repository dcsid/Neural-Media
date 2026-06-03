"""Async worker — kicks the HF Space and marks the job as downloading.

Invoked async (InvocationType=Event) from jobs_create and jobs_upload with
payload { "jobId": "<hex>" }. Does NOT block on HF inference: it POSTs to
the Space, which acknowledges immediately and then runs yt-dlp + ffmpeg +
TRIBE in the background. When the Space finishes it POSTs the result to
/v2/internal/hf-callback, which writes S3 + flips status to done.

For YouTube-URL jobs the analysis window (startSec/endSec) rides along to the
Space's /predict call; uploads carry no segment and are analyzed in full
(CONTRACTS §13.4 / §13.5).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from shared import (
    STATUS_DOWNLOADING,
    STATUS_FAILED_DOWNLOAD,
    get_callback_secret,
    get_job,
    now_epoch,
    presigned_get,
    update_job,
)

HF_SPACE_URL = os.environ["HF_SPACE_URL"].rstrip("/")
CALLBACK_URL = os.environ["CALLBACK_URL"]

# The HF Space's /predict acks immediately, so 10s is plenty for the round-trip
# even with a cold Space spin-up. Real inference time goes through the callback.
HF_KICK_TIMEOUT_SEC = 10


def _predict_payload(
    job_id: str, source_payload: dict, segment: dict, callback_token: str
) -> dict:
    """Body for the Space's POST /predict.

    ``segment`` carries top-level ``startSec``/``endSec`` for YouTube-URL jobs
    (CONTRACTS §13.5) and is empty for uploads, which the Space analyzes in
    full. Kept pure (no network) so it is unit-testable.
    """
    return {
        "jobId": job_id,
        "callbackUrl": CALLBACK_URL,
        "callbackToken": callback_token,
        **source_payload,
        **segment,
    }


def _kick_hf_space(job_id: str, source_payload: dict, segment: dict) -> None:
    body = json.dumps(
        # callbackToken is resolved at cold start from SSM SecureString and
        # memoised for the warm container's lifetime (see shared/).
        _predict_payload(job_id, source_payload, segment, get_callback_secret())
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{HF_SPACE_URL}/predict",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HF_KICK_TIMEOUT_SEC) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"HF Space returned HTTP {resp.status}")


def lambda_handler(event: dict, _context):
    job_id = event.get("jobId")
    if not job_id:
        # Async-invoke payload was malformed; no row to update.
        return {"ok": False, "error": "missing_job_id"}

    job = get_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}

    # Translate the stored source into the discriminated-union shape the
    # HF Space's pydantic PredictRequest expects:
    #   { "source": { "kind": "url" | "s3", "value": "<url>" } }
    # YouTube-URL jobs go to the Space as kind="url" (it validates + segments
    # them). Uploaded files go as kind="s3" with a presigned GET URL the Space
    # fetches verbatim — no YouTube validation, whole file (CONTRACTS §13.4/§13.5).
    # Sending an upload as kind="url" would fail the Space's YouTube validator.
    source = job.get("source")
    if source == "url":
        source_payload = {"source": {"kind": "url", "value": job["sourceUrl"]}}
    elif source == "s3":
        source_payload = {
            "source": {
                "kind": "s3",
                "value": presigned_get(job["uploadKey"], expires_in=3600),
            }
        }
    else:
        update_job(
            job_id,
            status=STATUS_FAILED_DOWNLOAD,
            error="unknown_source",
            updatedAt=now_epoch(),
        )
        return {"ok": False, "error": "unknown_source"}

    # Segment selection applies to the YouTube-URL path only; uploads are
    # analyzed in full (CONTRACTS §13.4). startSec/endSec come back from
    # DynamoDB as Decimal — cast to float for the JSON /predict body.
    segment: dict = {}
    if job.get("startSec") is not None and job.get("endSec") is not None:
        segment = {
            "startSec": float(job["startSec"]),
            "endSec": float(job["endSec"]),
        }

    update_job(job_id, status=STATUS_DOWNLOADING, updatedAt=now_epoch())

    try:
        _kick_hf_space(job_id, source_payload, segment)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
        update_job(
            job_id,
            status=STATUS_FAILED_DOWNLOAD,
            error=f"hf_space_unreachable: {e}",
            updatedAt=now_epoch(),
        )
        return {"ok": False, "error": "hf_space_unreachable"}

    return {"ok": True, "jobId": job_id}
