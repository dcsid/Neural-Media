"""POST /v2/internal/hf-callback — HF Space → AWS, secret-token auth.

Body schema (from the HF Space):
  {
    jobId: str,
    status: "inferring" | "done" | "failed_download" | "failed_inference" | "rejected_duration",
    stage?: str,            # advisory sub-stage (CONTRACTS §13.6), e.g. "encoding"
    progress?: float,       # coarse 0..1 completion fraction, anchored to real stages
    modelVersion?: str,
    durationSec?: float,    # video duration as measured by ffprobe on the Space
    activationsB64?: str,   # base64 of the gzip'd activation JSON; required iff status=="done"
    error?: str,
  }

The Space POSTs one or more intermediate `status="inferring"` callbacks carrying
`stage`/`progress` as it works, then exactly one terminal callback. Intermediate
callbacks make the multi-minute GPU phase observable to the polling frontend.
"""
from __future__ import annotations

import base64
import binascii
from decimal import Decimal

from shared import (
    JOB_STAGES,
    STATUS_DONE,
    STATUS_FAILED_DOWNLOAD,
    STATUS_FAILED_INFERENCE,
    STATUS_INFERRING,
    STATUS_REJECTED_DURATION,
    TERMINAL_STATUSES,
    clamp_progress,
    get_job,
    json_response,
    now_epoch,
    parse_body,
    update_job,
    verify_callback_token,
    write_result_gzip,
)

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
    if not verify_callback_token(_headers_lower(event)):
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
    # Advisory progress hints (CONTRACTS §13.6). Whitelist `stage` against the
    # known vocabulary and clamp `progress` to [0, 1] — both cross the Space→AWS
    # trust boundary, and neither affects the authoritative `status` machine.
    stage = body.get("stage")
    if isinstance(stage, str) and stage in JOB_STAGES:
        updates["stage"] = stage
    progress = clamp_progress(body.get("progress"))
    if progress is not None:
        # DynamoDB's resource layer rejects raw float — store as Decimal, same
        # as durationSec; jobs_status casts back to float on read.
        updates["progress"] = Decimal(str(progress))
    # The HF Space measures the actual decoded video duration with ffprobe
    # and reports it back so the frontend can render exact-length context
    # ("a 7.3-second clip") instead of guessing from the keyframe stride.
    # DynamoDB's resource layer rejects raw float — convert via Decimal(str(...))
    # to preserve the source decimal representation exactly.
    duration_sec = body.get("durationSec")
    if isinstance(duration_sec, (int, float)) and not isinstance(duration_sec, bool):
        updates["durationSec"] = Decimal(str(duration_sec))

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
