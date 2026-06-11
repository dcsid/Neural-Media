"""GET /v2/jobs/{id} — status projection, incl. the in-flight progress hints
(CONTRACTS §13.6) written by hf_callback."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from jobs_status import handler as js


def _event(job_id: str = "j1") -> dict:
    return {"pathParameters": {"jobId": job_id}}


def test_surfaces_stage_and_casts_progress_to_float(monkeypatch):
    """`progress` is stored as a DynamoDB Decimal; the response must cast it back
    to a JSON-native float, and `stage` passes through as-is."""
    monkeypatch.setattr(
        js, "get_job",
        lambda _id: {
            "jobId": "j1",
            "status": "inferring",
            "createdAt": 100,
            "stage": "encoding",
            "progress": Decimal("0.55"),
        },
    )
    monkeypatch.setattr(js, "now_epoch", lambda: 142)

    resp = js.lambda_handler(_event(), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["status"] == "inferring"
    assert body["stage"] == "encoding"
    assert body["progress"] == 0.55
    assert isinstance(body["progress"], float)
    assert body["elapsedSec"] == 42


def test_omits_progress_hints_when_absent(monkeypatch):
    """A pre-callback job row carries no stage/progress — the response omits the
    optional fields entirely rather than sending nulls."""
    monkeypatch.setattr(
        js, "get_job",
        lambda _id: {"jobId": "j1", "status": "pending", "createdAt": 100},
    )
    monkeypatch.setattr(js, "now_epoch", lambda: 105)

    resp = js.lambda_handler(_event(), None)

    body = json.loads(resp["body"])
    assert "stage" not in body
    assert "progress" not in body
