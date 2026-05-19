"""POST /v2/internal/hf-callback — HF Space → AWS, secret-token auth.

Body schema (from the HF Space):
  {
    jobId: str,
    status: "inferring" | "done" | "failed_download" | "failed_inference" | "rejected_duration",
    modelVersion?: str,
    activationsB64?: str,   # base64 of the gzip'd activation JSON; required iff status=="done"
    error?: str,
  }
"""
from __future__ import annotations

import base64
import binascii
import os

from shared import (
    STATUS_DONE,
    STATUS_FAILED_DOWNLOAD,
    STATUS_FAILED_INFERENCE,
    STATUS_INFERRING,
    STATUS_REJECTED_DURATION,
    TERMINAL_STATUSES,
    get_job,
    json_response,
    now_epoch,
    parse_body,
    update_job,
    verify_callback_token,
    write_result_gzip,
)

HF_CALLBACK_SECRET = os.environ["HF_CALLBACK_SECRET"]

_ACCEPTED_STATUSES = frozenset(
    {
        STATUS_INFERRING,
        STATUS_DONE,
        STATUS_FAILED_DOWNLOAD,
        STATUS_FAILED_INFERENCE,
        STATUS_REJECTED_DURATION,
    }
)


def _headers_lower(event: dict) -> dict:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def lambda_handler(event: dict, _context) -> dict:
    if not verify_callback_token(_headers_lower(event), HF_CALLBACK_SECRET):
        return json_response(401, {"error": "unauthorized"})

    body = parse_body(event)
    job_id = body.get("jobId")
    new_status = body.get("status")
    if not job_id or new_status not in _ACCEPTED_STATUSES:
        return json_response(400, {"error": "bad_request"})

    job = get_job(job_id)
    if not job:
        return json_response(404, {"error": "job_not_found"})

    # Guard against out-of-order callbacks. Once a job has reached a
    # terminal state, refuse to downgrade it back to a non-terminal one.
    # (Two terminal states overwriting each other is also wrong, but
    # we err toward "first terminal wins" implicitly — callers should
    # not retry once they've seen a 200 from us.)
    current = job.get("status")
    if current in TERMINAL_STATUSES and new_status not in TERMINAL_STATUSES:
        return json_response(200, {"ok": True, "noop": True})

    updates: dict = {"status": new_status, "updatedAt": now_epoch()}
    if body.get("modelVersion"):
        updates["modelVersion"] = body["modelVersion"]
    if body.get("error"):
        updates["error"] = body["error"]

    if new_status == STATUS_DONE:
        activations_b64 = body.get("activationsB64")
        if not activations_b64:
            return json_response(400, {"error": "missing_activations"})
        try:
            gz_bytes = base64.b64decode(activations_b64, validate=True)
        except (ValueError, binascii.Error):
            return json_response(400, {"error": "invalid_activations_b64"})
        result_key = f"results/{job_id}.json.gz"
        write_result_gzip(result_key, gz_bytes)
        updates["resultS3Key"] = result_key

    update_job(job_id, **updates)
    return json_response(200, {"ok": True})
