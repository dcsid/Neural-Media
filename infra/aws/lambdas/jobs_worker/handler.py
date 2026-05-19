"""Async worker — kicks the HF Space and marks the job as downloading.

Invoked async (InvocationType=Event) from jobs_create and jobs_upload with
payload { "jobId": "<hex>" }. Does NOT block on HF inference: it POSTs to
the Space, which acknowledges immediately and then runs yt-dlp + ffmpeg +
TRIBE in the background. When the Space finishes it POSTs the result to
/v2/internal/hf-callback, which writes S3 + flips status to done.
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


def _kick_hf_space(job_id: str, source_payload: dict) -> None:
    body = json.dumps(
        {
            "jobId": job_id,
            "callbackUrl": CALLBACK_URL,
            # Resolved at cold start from SSM SecureString; memoised
            # for the warm container's lifetime (see shared/).
            "callbackToken": get_callback_secret(),
            **source_payload,
        }
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
    # We always tag kind="url" because S3 paths get presigned to URLs above,
    # and the Space's url and s3 source handlers do the same thing once given
    # an HTTP URL. Keeping the discriminator field around leaves room to
    # eventually let the Space differentiate (e.g., to skip the redownload).
    source = job.get("source")
    if source == "url":
        source_payload = {"source": {"kind": "url", "value": job["sourceUrl"]}}
    elif source == "s3":
        source_payload = {
            "source": {
                "kind": "url",
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

    update_job(job_id, status=STATUS_DOWNLOADING, updatedAt=now_epoch())

    try:
        _kick_hf_space(job_id, source_payload)
    except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
        update_job(
            job_id,
            status=STATUS_FAILED_DOWNLOAD,
            error=f"hf_space_unreachable: {e}",
            updatedAt=now_epoch(),
        )
        return {"ok": False, "error": "hf_space_unreachable"}

    return {"ok": True, "jobId": job_id}
