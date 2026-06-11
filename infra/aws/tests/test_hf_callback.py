"""hf_callback consumes the renamed block code (CONTRACTS §13.5).

The callback stores the Space's ``error`` field verbatim, so the
``tiktok_blocked`` → ``download_blocked`` rename needs no special-casing
here — this test guards that the generic code passes straight through.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

from hf_callback import handler as cb


def test_download_blocked_error_passes_through(monkeypatch):
    monkeypatch.setattr(cb, "verify_callback_token", lambda _h: True)
    monkeypatch.setattr(
        cb, "get_job", lambda _id: {"jobId": "j1", "status": "downloading"}
    )
    upd = mock.MagicMock(name="update_job")
    monkeypatch.setattr(cb, "update_job", upd)

    event = {
        "headers": {"x-nm-token": "secret"},
        "body": json.dumps(
            {"jobId": "j1", "status": "failed_download", "error": "download_blocked"}
        ),
    }
    resp = cb.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    kwargs = upd.call_args.kwargs
    assert kwargs["status"] == "failed_download"
    assert kwargs["error"] == "download_blocked"


def test_unauthorized_callback_is_rejected(monkeypatch):
    monkeypatch.setattr(cb, "verify_callback_token", lambda _h: False)
    resp = cb.lambda_handler({"headers": {}, "body": "{}"}, None)
    assert resp["statusCode"] == 401


def test_intermediate_inferring_callback_stores_stage_and_progress(monkeypatch):
    """An in-flight progress ping (CONTRACTS §13.6) updates stage + progress
    without requiring activations, and is stored as a Decimal for DynamoDB."""
    monkeypatch.setattr(cb, "verify_callback_token", lambda _h: True)
    monkeypatch.setattr(
        cb, "get_job", lambda _id: {"jobId": "j1", "status": "downloading"}
    )
    upd = mock.MagicMock(name="update_job")
    monkeypatch.setattr(cb, "update_job", upd)

    event = {
        "headers": {"x-nm-token": "secret"},
        "body": json.dumps(
            {"jobId": "j1", "status": "inferring", "stage": "encoding", "progress": 0.55}
        ),
    }
    resp = cb.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    kwargs = upd.call_args.kwargs
    assert kwargs["status"] == "inferring"
    assert kwargs["stage"] == "encoding"
    assert kwargs["progress"] == Decimal("0.55")


def test_unknown_stage_dropped_and_progress_clamped(monkeypatch):
    """`stage` is whitelisted and `progress` clamped to [0, 1] — both cross the
    Space→AWS trust boundary, so a garbage stage / out-of-range fraction must not
    corrupt the job row."""
    monkeypatch.setattr(cb, "verify_callback_token", lambda _h: True)
    monkeypatch.setattr(
        cb, "get_job", lambda _id: {"jobId": "j1", "status": "inferring"}
    )
    upd = mock.MagicMock(name="update_job")
    monkeypatch.setattr(cb, "update_job", upd)

    event = {
        "headers": {"x-nm-token": "secret"},
        "body": json.dumps(
            {"jobId": "j1", "status": "inferring", "stage": "bogus", "progress": 1.7}
        ),
    }
    resp = cb.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    kwargs = upd.call_args.kwargs
    assert "stage" not in kwargs  # unknown stage rejected
    assert kwargs["progress"] == Decimal("1.0")  # clamped into range


def test_intermediate_callback_cannot_downgrade_terminal_job(monkeypatch):
    """A late progress ping that races the terminal callback is a no-op — it must
    not knock a `done` job back to `inferring`."""
    monkeypatch.setattr(cb, "verify_callback_token", lambda _h: True)
    monkeypatch.setattr(cb, "get_job", lambda _id: {"jobId": "j1", "status": "done"})
    upd = mock.MagicMock(name="update_job")
    monkeypatch.setattr(cb, "update_job", upd)

    event = {
        "headers": {"x-nm-token": "secret"},
        "body": json.dumps(
            {"jobId": "j1", "status": "inferring", "stage": "aggregating", "progress": 0.9}
        ),
    }
    resp = cb.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    assert resp["body"] and json.loads(resp["body"]).get("noop") is True
    upd.assert_not_called()
