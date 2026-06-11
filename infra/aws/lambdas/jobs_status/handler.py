"""GET /v2/jobs/{jobId} — return current job state, plus presigned result URL when done."""
from __future__ import annotations

from shared import (
    STATUS_DONE,
    get_job,
    json_response,
    now_epoch,
    presigned_get,
)


def lambda_handler(event: dict, _context) -> dict:
    job_id = (event.get("pathParameters") or {}).get("jobId")
    if not job_id:
        return json_response(400, {"error": "missing_job_id"})

    job = get_job(job_id)
    if not job:
        return json_response(404, {"error": "job_not_found"})

    created_at = int(job.get("createdAt", 0))
    elapsed_sec = max(0, now_epoch() - created_at) if created_at else None

    body: dict = {
        "jobId": job["jobId"],
        "status": job.get("status"),
        "createdAt": created_at,
        "elapsedSec": elapsed_sec,
    }
    if job.get("error"):
        body["error"] = job["error"]
    if job.get("modelVersion"):
        body["modelVersion"] = job["modelVersion"]
    # Advisory progress hints written by hf_callback from the Space's
    # intermediate callbacks (CONTRACTS §13.6). `stage` is a plain string;
    # `progress` is stored as a DynamoDB Decimal — cast back to float so
    # json_response can serialize it.
    if job.get("stage"):
        body["stage"] = job["stage"]
    if job.get("progress") is not None:
        body["progress"] = float(job["progress"])
    # hf_callback writes durationSec as a DynamoDB Decimal; cast back to
    # float so json.dumps() in json_response handles it natively (Decimal
    # is not JSON-serializable by default).
    if job.get("durationSec") is not None:
        body["durationSec"] = float(job["durationSec"])
    if job.get("status") == STATUS_DONE and job.get("resultS3Key"):
        body["resultUrl"] = presigned_get(job["resultS3Key"], expires_in=3600)
    return json_response(200, body)
