"""Shared helpers for every Lambda in the control plane.

Bundled into each function's deployment package (CodeUri: lambdas/), so
keep imports minimal — extra dependencies inflate every cold start.
"""
from __future__ import annotations

import base64
import binascii
import functools
import hmac
import json
import os
import time
import uuid
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

# Required env vars (set by the SAM template's Globals.Function.Environment).
JOBS_TABLE: str = os.environ["JOBS_TABLE"]
RESULTS_BUCKET: str = os.environ["RESULTS_BUCKET"]
FRONTEND_ORIGIN: str = os.environ.get("FRONTEND_ORIGIN", "*")

# Lazy boto3 clients. Initialised at import to share a single connection
# pool across warm invocations.
_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(JOBS_TABLE)
_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")
_ssm = boto3.client("ssm")

# Job lifecycle. Matches the API contract in docs/single-video.md.
STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_INFERRING = "inferring"
STATUS_DONE = "done"
STATUS_FAILED_DOWNLOAD = "failed_download"
STATUS_FAILED_INFERENCE = "failed_inference"
STATUS_REJECTED_DURATION = "rejected_duration"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_DONE,
        STATUS_FAILED_DOWNLOAD,
        STATUS_FAILED_INFERENCE,
        STATUS_REJECTED_DURATION,
    }
)

# How long a job row sticks around before DynamoDB's TTL deletes it.
JOB_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Identifiers, time helpers.
# ---------------------------------------------------------------------------
def new_job_id() -> str:
    """Opaque, URL-safe job identifier. uuid4 hex is 32 chars, plenty of
    entropy and trivially safe in a path segment."""
    return uuid.uuid4().hex


def now_epoch() -> int:
    return int(time.time())


def default_expires_at() -> int:
    return now_epoch() + JOB_TTL_DAYS * 24 * 3600


# ---------------------------------------------------------------------------
# DynamoDB job-row helpers.
# ---------------------------------------------------------------------------
def put_job(item: dict) -> None:
    _table.put_item(Item=item)


def get_job(job_id: str) -> Optional[dict]:
    try:
        resp = _table.get_item(Key={"jobId": job_id})
    except ClientError:
        raise
    return resp.get("Item")


def update_job(job_id: str, **fields: Any) -> None:
    """Set arbitrary attributes on a job. UpdateExpression is built
    dynamically so the caller doesn't have to think about reserved-word
    collisions (`status`, `source`, etc. are all DynamoDB reserved words).
    """
    if not fields:
        return
    expr_parts = []
    values: dict[str, Any] = {}
    names: dict[str, str] = {}
    for i, (k, v) in enumerate(fields.items()):
        name_key = f"#n{i}"
        val_key = f":v{i}"
        names[name_key] = k
        values[val_key] = v
        expr_parts.append(f"{name_key} = {val_key}")
    _table.update_item(
        Key={"jobId": job_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ---------------------------------------------------------------------------
# Async invoke of the worker Lambda.
# ---------------------------------------------------------------------------
def async_invoke_worker(job_id: str, worker_function_name: str) -> None:
    _lambda.invoke(
        FunctionName=worker_function_name,
        InvocationType="Event",
        Payload=json.dumps({"jobId": job_id}).encode(),
    )


# ---------------------------------------------------------------------------
# S3 presign / write helpers.
# ---------------------------------------------------------------------------
def presigned_put(
    key: str, expires_in: int = 900, content_type: str = "video/mp4"
) -> str:
    return _s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": RESULTS_BUCKET,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def presigned_get(key: str, expires_in: int = 3600) -> str:
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": RESULTS_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )


def write_result_gzip(key: str, gz_bytes: bytes) -> None:
    """Write the gzip-encoded activation JSON directly to S3. We don't
    re-gzip in the Lambda — the HF Space already gzipped it, so storing
    as-is is faster and saves CPU. ContentEncoding: gzip lets browsers
    auto-decompress when they fetch via the presigned URL."""
    _s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=gz_bytes,
        ContentType="application/json",
        ContentEncoding="gzip",
    )


# ---------------------------------------------------------------------------
# Callback auth.
# ---------------------------------------------------------------------------
# The shared callback secret lives in SSM SecureString (set
# out-of-band; see infra/aws/README.md). We can't pass it via
# resolve:ssm-secure in a Lambda env var because that dynamic
# reference isn't on CFN's Lambda-env whitelist — it would land as
# the literal "{{resolve:...}}" string. So fetch at cold start and
# memoise for the lifetime of the warm container; the +1 SSM call
# costs ~50 ms once per cold start.
@functools.lru_cache(maxsize=1)
def get_callback_secret() -> str:
    name = os.environ["HF_CALLBACK_SECRET_SSM_NAME"]
    version = os.environ.get("HF_CALLBACK_SECRET_SSM_VERSION") or ""
    # SSM accepts "{name}:{version}" to pin a specific version. Empty
    # / missing means "latest" — keep this default so deploys without
    # an explicit version still work.
    if version:
        name = f"{name}:{version}"
    return _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def verify_callback_token(headers: dict) -> bool:
    """Constant-time compare against the SSM-resolved shared secret.

    API Gateway HTTP API lowercases header keys, but allow either
    form for tooling that doesn't. Returns False on missing/empty
    header rather than raising — handlers map to 401.
    """
    expected = get_callback_secret()
    if not expected:
        return False
    provided = headers.get("x-nm-token") or headers.get("X-NM-Token") or ""
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# API Gateway HTTP API v2 response helpers.
# ---------------------------------------------------------------------------
def _response_headers() -> dict[str, str]:
    # CORS is handled at the API Gateway level (HttpApi.CorsConfiguration);
    # we don't re-emit Access-Control-* here. Setting them in the Lambda
    # response in addition would cause some browsers to refuse the response
    # due to duplicate headers.
    return {"Content-Type": "application/json"}


def json_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": _response_headers(),
        "body": json.dumps(body),
    }


def parse_body(event: dict) -> dict:
    """Decode the API Gateway HTTP API v2 request body to a JSON dict.
    Returns {} on empty body or parse failure — handlers should validate
    fields explicitly rather than trust this to short-circuit."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
