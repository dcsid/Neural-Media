"""Async worker — kicks the HF Space and marks the job as downloading.

Invoked async (InvocationType=Event) from jobs_create and jobs_upload with
payload { "jobId": "<hex>" }. Does NOT block on HF inference: it POSTs to
the Space, which acknowledges immediately and then runs yt-dlp + ffmpeg +
TRIBE in the background. When the Space finishes it POSTs the result to
/v2/internal/hf-callback, which writes S3 + flips status to done.

The analysis window (startSec/endSec) rides along to the Space's /predict call
for BOTH url and upload jobs when present — the Space trims to it; absent (a
whole-file upload) means no segment (CONTRACTS §13.4 / §13.5).
"""
from __future__ import annotations

import json
import os
import time
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

# The Space's /predict acks immediately — but ONLY once the Space is awake. A
# sleeping Space (gcTimeout sleep) cold-boots in ~1-2 min, during which HF holds
# the connection open with no response; a short timeout there marks perfectly
# healthy jobs `hf_space_unreachable`. So we wake + confirm readiness via
# /healthz polling (cold start tolerated) BEFORE POSTing /predict to the ready
# Space. NB: the worker Lambda's Timeout must exceed
# HF_WAKE_BUDGET_SEC + HF_KICK_TIMEOUT_SEC (see template.yaml).
HF_KICK_TIMEOUT_SEC = 15      # POST /predict round-trip once the Space is up
HF_WAKE_BUDGET_SEC = 180      # total time allowed to cold-boot a sleeping Space
HF_HEALTH_TIMEOUT_SEC = 10    # per /healthz probe
HF_HEALTH_INTERVAL_SEC = 5    # pause between probes


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


def _space_is_healthy() -> bool:
    """One /healthz probe. The first probe to a sleeping Space triggers HF's
    cold boot; this returns False (timeout / 5xx) until the app is actually up."""
    try:
        with urllib.request.urlopen(
            f"{HF_SPACE_URL}/healthz", timeout=HF_HEALTH_TIMEOUT_SEC
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _ensure_space_awake() -> None:
    """Block until /healthz is 200 or the wake budget elapses. Tolerates a cold
    gcTimeout wake (~1-2 min) so the subsequent /predict lands on a ready Space
    instead of timing out mid-boot. Raises TimeoutError if it never comes up."""
    deadline = time.monotonic() + HF_WAKE_BUDGET_SEC
    while not _space_is_healthy():
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Space not ready after {HF_WAKE_BUDGET_SEC}s (cold start)"
            )
        time.sleep(HF_HEALTH_INTERVAL_SEC)


def _kick_hf_space(job_id: str, source_payload: dict, segment: dict) -> None:
    # Wake + confirm the Space is up before POSTing, so a cold gcTimeout sleep
    # doesn't time out the /predict kick (the bug that wrongly marked warm-able
    # jobs hf_space_unreachable).
    _ensure_space_awake()
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

    # The analysis window applies to both url and upload jobs (the Space trims
    # the upload to it); absent → whole file. startSec/endSec come back from
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
