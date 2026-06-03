"""jobs_worker threads the segment to the Space (CONTRACTS §13.5)."""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from jobs_worker import handler as jw


# --- pure payload builder --------------------------------------------------


def test_predict_payload_includes_segment_for_url():
    payload = jw._predict_payload(
        "job1",
        {"source": {"kind": "url", "value": "https://youtu.be/x"}},
        {"startSec": 12.0, "endSec": 78.0},
        "tok",
    )
    assert payload["jobId"] == "job1"
    assert payload["callbackToken"] == "tok"
    assert payload["source"] == {"kind": "url", "value": "https://youtu.be/x"}
    # startSec/endSec are top-level, alongside source/callbackUrl/callbackToken.
    assert payload["startSec"] == 12.0
    assert payload["endSec"] == 78.0


def test_predict_payload_omits_segment_when_empty():
    payload = jw._predict_payload(
        "job1",
        {"source": {"kind": "url", "value": "https://signed/upload"}},
        {},  # uploads carry no segment
        "tok",
    )
    assert "startSec" not in payload
    assert "endSec" not in payload


# --- handler dispatch ------------------------------------------------------


def test_worker_threads_segment_from_url_job(monkeypatch):
    job = {
        "jobId": "j1",
        "source": "url",
        "sourceUrl": "https://youtu.be/x",
        # stored by jobs_create as Decimal
        "startSec": Decimal("12"),
        "endSec": Decimal("78"),
    }
    monkeypatch.setattr(jw, "get_job", lambda _id: job)
    monkeypatch.setattr(jw, "update_job", mock.MagicMock())
    kick = mock.MagicMock(name="_kick_hf_space")
    monkeypatch.setattr(jw, "_kick_hf_space", kick)

    out = jw.lambda_handler({"jobId": "j1"}, None)

    assert out["ok"] is True
    _job_id, source_payload, segment = kick.call_args.args
    assert source_payload["source"]["kind"] == "url"
    # Decimal → float on the way to the JSON body.
    assert segment == {"startSec": 12.0, "endSec": 78.0}


def test_worker_sends_no_segment_for_upload_job(monkeypatch):
    job = {"jobId": "j2", "source": "s3", "uploadKey": "uploads/j2.mp4"}
    monkeypatch.setattr(jw, "get_job", lambda _id: job)
    monkeypatch.setattr(jw, "update_job", mock.MagicMock())
    monkeypatch.setattr(jw, "presigned_get", lambda *a, **k: "https://signed/...")
    kick = mock.MagicMock(name="_kick_hf_space")
    monkeypatch.setattr(jw, "_kick_hf_space", kick)

    jw.lambda_handler({"jobId": "j2"}, None)

    _job_id, _source_payload, segment = kick.call_args.args
    assert segment == {}  # uploads are analyzed in full (§13.4)


def test_worker_missing_job_id_short_circuits():
    assert jw.lambda_handler({}, None) == {"ok": False, "error": "missing_job_id"}
