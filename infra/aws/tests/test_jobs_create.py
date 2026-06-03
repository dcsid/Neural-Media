"""POST /v2/jobs — YouTube URL + segment validation (CONTRACTS §13.2)."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

import pytest

from jobs_create import handler as jc

_VALID_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _event(body) -> dict:
    raw = body if isinstance(body, str) else json.dumps(body)
    return {"body": raw}


@pytest.fixture
def patched(monkeypatch):
    """Stub the two AWS-touching helpers; return (put_job, async_invoke)."""
    put = mock.MagicMock(name="put_job")
    invoke = mock.MagicMock(name="async_invoke_worker")
    monkeypatch.setattr(jc, "put_job", put)
    monkeypatch.setattr(jc, "async_invoke_worker", invoke)
    return put, invoke


# --- URL validation --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=abc123",
        "https://m.youtube.com/watch?v=abc123",
        "http://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc123",
    ],
)
def test_accepts_youtube_forms(patched, url):
    put, invoke = patched
    resp = jc.lambda_handler(_event({"url": url, "startSec": 0, "endSec": 30}), None)
    assert resp["statusCode"] == 201, resp
    assert "jobId" in json.loads(resp["body"])
    put.assert_called_once()
    invoke.assert_called_once()


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@u/video/123",  # TikTok no longer accepted
        "https://vimeo.com/12345",
        "https://www.youtube.com/",  # no video
        "https://www.youtube.com/watch",  # missing v=
        "https://youtu.be/",  # missing id
        "https://evil.com/watch?v=x",
        "ftp://youtube.com/watch?v=x",  # wrong scheme
        "not-a-url",
        "",
        None,
        12345,  # not even a string
    ],
)
def test_rejects_non_youtube_url(patched, url):
    put, invoke = patched
    resp = jc.lambda_handler(_event({"url": url, "startSec": 0, "endSec": 30}), None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error_code"] == "invalid_url"
    put.assert_not_called()
    invoke.assert_not_called()


# --- segment validation ----------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (-1, 30),  # start < 0
        (30, 30),  # start >= end (equal)
        (40, 30),  # start > end
        ("0", "30"),  # non-numeric (strings)
        (None, 30),  # missing start
        (0, None),  # missing end
        (True, 30),  # bool is not a valid number
        (float("nan"), 30),  # non-finite
        (float("inf"), 30),
    ],
)
def test_bad_segment(patched, start, end):
    put, _ = patched
    resp = jc.lambda_handler(
        _event({"url": _VALID_URL, "startSec": start, "endSec": end}), None
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error_code"] == "bad_segment"
    put.assert_not_called()


def test_segment_too_long(patched):
    put, _ = patched
    resp = jc.lambda_handler(
        _event({"url": _VALID_URL, "startSec": 0, "endSec": 91}), None
    )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error_code"] == "segment_too_long"
    put.assert_not_called()


def test_segment_exactly_90_is_allowed(patched):
    # 95 - 5 == 90, which is the cap, not over it.
    resp = jc.lambda_handler(
        _event({"url": _VALID_URL, "startSec": 5, "endSec": 95}), None
    )
    assert resp["statusCode"] == 201


# --- validation order: url is checked before the segment -------------------


def test_invalid_url_takes_precedence_over_bad_segment(patched):
    resp = jc.lambda_handler(
        _event({"url": "https://tiktok.com/x", "startSec": 50, "endSec": 10}), None
    )
    assert json.loads(resp["body"])["error_code"] == "invalid_url"


# --- record shape ----------------------------------------------------------


def test_stores_segment_as_decimal_with_jobid(patched):
    put, invoke = patched
    resp = jc.lambda_handler(
        _event({"url": _VALID_URL, "startSec": 12.5, "endSec": 78}), None
    )
    assert resp["statusCode"] == 201
    item = put.call_args.args[0]
    assert item["source"] == "url"
    assert item["sourceUrl"] == _VALID_URL
    assert item["status"] == "pending"
    # Stored as Decimal so the DynamoDB resource layer accepts them.
    assert item["startSec"] == Decimal("12.5")
    assert item["endSec"] == Decimal("78")
    # The worker is kicked with the freshly minted job id.
    assert invoke.call_args.args[0] == json.loads(resp["body"])["jobId"]


def test_malformed_json_body_is_invalid_url(patched):
    # parse_body returns {} on junk → url is None → invalid_url.
    resp = jc.lambda_handler({"body": "{not valid json"}, None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error_code"] == "invalid_url"
