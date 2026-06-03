"""hf_callback consumes the renamed block code (CONTRACTS §13.5).

The callback stores the Space's ``error`` field verbatim, so the
``tiktok_blocked`` → ``download_blocked`` rename needs no special-casing
here — this test guards that the generic code passes straight through.
"""
from __future__ import annotations

import json
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
