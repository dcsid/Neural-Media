"""POST /v2/jobs — create a job from a YouTube URL + analysis segment.

Returns 201 { jobId } immediately; the actual download + inference runs
asynchronously in jobs_worker, which the HF Space then calls back.

Per shared/CONTRACTS.md §13 the request is { url, startSec, endSec } and the
job analyzes only the [startSec, endSec) window. Cheap, request-only checks
reject synchronously with HTTP 400 + an error code:

  invalid_url       — url is not one of the accepted YouTube forms
  bad_segment       — startSec/endSec missing/non-numeric, or start<0, or start>=end
  segment_too_long  — endSec - startSec > 90 (the hard cap)

The remaining rule (endSec <= real video duration → segment_out_of_bounds)
needs the actual video length, so it is authoritative in the Space and
surfaces later as a terminal failed status — never here.
"""
from __future__ import annotations

import math
import os
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

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

# 2 KB cap on source URLs — real YouTube URLs are well under this and anything
# longer is almost certainly an attempt to stuff junk through.
_MAX_URL_LEN = 2048

# Hard ceiling on the analysis window (CONTRACTS.md §13.2). The model + the
# cost/latency budget are built around short stimuli; there is no auto-trim.
_MAX_SEGMENT_SEC = 90.0

# Accepted YouTube hosts (CONTRACTS.md §13.1 / §13.5). TikTok and every other
# host are rejected as invalid_url — this product is YouTube-only now.
_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
)


def _is_valid_youtube_url(url: object) -> bool:
    """True iff ``url`` is one of the accepted YouTube forms (§13.5):
    ``youtube.com/watch?v=…`` (+ ``www.``/``m.`` variants), ``youtu.be/<id>``,
    or ``youtube.com/shorts/<id>``. Anything else → ``invalid_url``."""
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LEN:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()  # hostname strips port + userinfo
    if host not in _YOUTUBE_HOSTS:
        return False
    path = parsed.path or ""
    if host == "youtu.be":
        # youtu.be/<id> — a non-empty id segment after the slash.
        return len(path) > 1
    # youtube.com family: /watch?v=<id> or /shorts/<id>.
    if path == "/watch":
        return bool(parse_qs(parsed.query).get("v"))
    if path.startswith("/shorts/"):
        return len(path) > len("/shorts/")
    return False


def _is_finite_number(x: object) -> bool:
    # bool is a subclass of int — exclude it; NaN/Inf are not a real segment.
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
    )


def _segment_error(start: object, end: object) -> str | None:
    """Return the error_code for an invalid ``[start, end)`` segment, else None.

    Mirrors the synchronous rows of CONTRACTS.md §13.2.
    """
    if not _is_finite_number(start) or not _is_finite_number(end):
        return "bad_segment"
    if start < 0 or start >= end:
        return "bad_segment"
    if end - start > _MAX_SEGMENT_SEC:
        return "segment_too_long"
    return None


def lambda_handler(event: dict, _context) -> dict:
    body = parse_body(event)

    url = body.get("url")
    if not _is_valid_youtube_url(url):
        return json_response(400, {"error": "invalid_url"})

    start_sec = body.get("startSec")
    end_sec = body.get("endSec")
    seg_err = _segment_error(start_sec, end_sec)
    if seg_err is not None:
        return json_response(400, {"error": seg_err})

    job_id = new_job_id()
    now = now_epoch()
    put_job(
        {
            "jobId": job_id,
            "status": STATUS_PENDING,
            "source": "url",
            "sourceUrl": url,
            # DynamoDB's resource layer rejects raw float — store as Decimal
            # (the str() form preserves the client's exact value). jobs_worker
            # casts back to float for the Space /predict payload.
            "startSec": Decimal(str(start_sec)),
            "endSec": Decimal(str(end_sec)),
            "createdAt": now,
            "updatedAt": now,
            "expiresAt": default_expires_at(),
        }
    )
    async_invoke_worker(job_id, WORKER_FUNCTION_NAME)
    return json_response(201, {"jobId": job_id})
