"""POST /v2/jobs — create a job from a TikTok / YouTube / arbitrary URL.

Returns 201 { jobId } immediately; the actual download + inference runs
asynchronously in jobs_worker, which the HF Space then calls back.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from shared import (
    STATUS_PENDING,
    async_invoke_worker,
    default_expires_at,
    json_response,
    new_job_id,
    now_epoch,
    parse_body,
    put_job,
)

WORKER_FUNCTION_NAME = os.environ["WORKER_FUNCTION_NAME"]

# 2 KB cap on source URLs — TikTok / YouTube URLs are well under this and
# anything longer is almost certainly an attempt to stuff junk through.
_MAX_URL_LEN = 2048


def _is_valid_url(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def lambda_handler(event: dict, _context) -> dict:
    body = parse_body(event)
    url = body.get("url")
    if not _is_valid_url(url):
        return json_response(400, {"error": "invalid_url"})

    job_id = new_job_id()
    now = now_epoch()
    put_job(
        {
            "jobId": job_id,
            "status": STATUS_PENDING,
            "source": "url",
            "sourceUrl": url,
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": default_expires_at(),
        }
    )
    async_invoke_worker(job_id, WORKER_FUNCTION_NAME)
    return json_response(201, {"jobId": job_id})
