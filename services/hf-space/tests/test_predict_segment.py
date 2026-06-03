"""Tests for the YouTube + segment /predict surface (CONTRACTS.md §13).

Covers the two things the brief calls out — request validation and the
`--download-sections` command construction — plus the generic block
classification. All pure / TestClient-level: no yt-dlp, ffmpeg, GPU, or
network is touched (valid requests that would spawn a real job are never
submitted).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app


# ---------------------------------------------------------------------------
# YouTube URL validation (§13.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtube.com/watch?v=abc123",
    "https://m.youtube.com/watch?v=abc123",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/abc123",
    "http://youtube.com/watch?v=x&t=5s",
])
def test_accepts_youtube_urls(url):
    assert app._is_youtube_url(url) is True


@pytest.mark.parametrize("url", [
    "https://www.tiktok.com/@nasa/video/12345",   # TikTok no longer accepted
    "https://vimeo.com/12345",
    "https://www.youtube.com/",                   # no video
    "https://www.youtube.com/watch",              # missing v=
    "https://example.com/watch?v=abc",            # wrong host
    "ftp://youtube.com/watch?v=abc",              # wrong scheme
    "not a url",
    "",
])
def test_rejects_non_youtube_urls(url):
    assert app._is_youtube_url(url) is False


# ---------------------------------------------------------------------------
# Segment validation (§13.2) — pure function
# ---------------------------------------------------------------------------
def _url_req(value="https://www.youtube.com/watch?v=abc123", start=12.0, end=78.0):
    return app.PredictRequest(
        jobId="job-1",
        source={"kind": "url", "value": value},
        callbackUrl="https://cb.example/cb",
        callbackToken="tok",
        startSec=start,
        endSec=end,
    )


def test_valid_segment_passes():
    assert app._segment_request_error(_url_req()) is None


def test_invalid_url_rejected():
    err = app._segment_request_error(_url_req(value="https://vimeo.com/1"))
    assert err is not None and err[0] == "invalid_url"


@pytest.mark.parametrize("start,end", [
    (-1.0, 30.0),   # startSec < 0
    (30.0, 30.0),   # startSec == endSec
    (40.0, 30.0),   # startSec > endSec
])
def test_bad_segment_rejected(start, end):
    err = app._segment_request_error(_url_req(start=start, end=end))
    assert err is not None and err[0] == "bad_segment"


def test_missing_segment_rejected():
    err = app._segment_request_error(_url_req(start=None, end=None))
    assert err is not None and err[0] == "bad_segment"


def test_segment_too_long_rejected():
    err = app._segment_request_error(_url_req(start=0.0, end=app.MAX_DURATION_SEC + 0.5))
    assert err is not None and err[0] == "segment_too_long"


def test_segment_exactly_max_ok():
    assert app._segment_request_error(_url_req(start=0.0, end=app.MAX_DURATION_SEC)) is None


def test_s3_source_skips_segment_validation():
    req = app.PredictRequest(
        jobId="job-2",
        source={"kind": "s3", "value": "https://s3.example/presigned"},
        callbackUrl="https://cb.example/cb",
        callbackToken="tok",
        # no segment — the upload path ignores it
    )
    assert app._segment_request_error(req) is None


# ---------------------------------------------------------------------------
# yt-dlp command construction
# ---------------------------------------------------------------------------
def test_download_sections_arg_format():
    assert app._download_sections_arg(12.0, 78.0) == "*12-78"
    assert app._download_sections_arg(12.5, 78.25) == "*12.5-78.25"
    assert app._download_sections_arg(0.0, 90.0) == "*0-90"


def test_download_cmd_limits_to_segment():
    cmd = app._ytdlp_download_cmd(
        "https://youtu.be/abc", "/tmp/video.%(ext)s", ua="UA", start=12.0, end=78.0
    )
    assert cmd[0] == "yt-dlp"
    assert "--download-sections" in cmd
    assert cmd[cmd.index("--download-sections") + 1] == "*12-78"
    # tight cuts + the URL present
    assert "--force-keyframes-at-cuts" in cmd
    assert "https://youtu.be/abc" in cmd


def test_duration_cmd_is_metadata_only():
    cmd = app._ytdlp_duration_cmd("https://youtu.be/abc", ua="UA")
    assert "--skip-download" in cmd
    assert cmd[cmd.index("--print") + 1] == "duration"
    assert "--download-sections" not in cmd     # never pulls media


# ---------------------------------------------------------------------------
# Block classification (download_blocked)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tail", [
    "ERROR: Video unavailable",
    "HTTP Error 403: Forbidden",
    "Sign in to confirm you're not a bot",
    "ERROR: Private video. Sign in if you've been granted access",
])
def test_block_signatures_detected(tail):
    assert app._looks_blocked(tail) is True


def test_transient_error_is_not_a_block():
    assert app._looks_blocked("Temporary failure in name resolution") is False


# ---------------------------------------------------------------------------
# Endpoint wiring — synchronous 400 + error_code (no job is spawned)
# ---------------------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    # The handler 503s unless the shared secret is set; set it so we reach
    # the segment validation. Bad requests 400 *before* any job is scheduled.
    monkeypatch.setattr(app, "CALLBACK_SHARED_SECRET", "test-secret")
    return TestClient(app.app)


def _body(value="https://www.youtube.com/watch?v=abc", start=12.0, end=78.0, kind="url"):
    body = {
        "jobId": "job-1",
        "source": {"kind": kind, "value": value},
        "callbackUrl": "https://cb.example/cb",
        "callbackToken": "tok",
    }
    if start is not None:
        body["startSec"] = start
    if end is not None:
        body["endSec"] = end
    return body


@pytest.mark.parametrize("body,code", [
    (_body(value="https://vimeo.com/1"), "invalid_url"),
    (_body(start=50.0, end=20.0), "bad_segment"),
    (_body(start=None, end=None), "bad_segment"),
    (_body(start=0.0, end=200.0), "segment_too_long"),
])
def test_predict_rejects_with_error_code(client, body, code):
    resp = client.post("/predict", json=body)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_code"] == code


def test_predict_503_without_secret(monkeypatch):
    monkeypatch.setattr(app, "CALLBACK_SHARED_SECRET", "")
    resp = TestClient(app.app).post("/predict", json=_body())
    assert resp.status_code == 503
