"""In-flight progress emission (CONTRACTS §13.6).

Covers the `_ProgressLogBridge` that turns TRIBE's own progress logs into
Space→AWS progress pings: that known markers map to the right stage, and that we
only fire on stage *transitions* (not once per log record). `_fire_progress`
itself is monkeypatched to a recorder, so no network or threads run.
"""
from __future__ import annotations

import logging

import app


def _record(name: str, msg: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=logging.INFO, pathname=__file__, lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def _bridge_with_recorder(monkeypatch):
    fired: list[str] = []
    monkeypatch.setattr(
        app, "_fire_progress",
        lambda url, token, job_id, stage: fired.append(stage),
    )
    bridge = app._ProgressLogBridge("http://cb", "tok", "j1")
    return bridge, fired


def test_known_markers_map_to_stages(monkeypatch):
    bridge, fired = _bridge_with_recorder(monkeypatch)

    bridge.emit(_record("tribev2.eventstransforms", "Running whisperx via uvx..."))
    bridge.emit(_record("tribev2.main", "Preparing extractor: video"))

    assert fired == ["transcribing", "encoding"]


def test_only_fires_on_stage_transition(monkeypatch):
    bridge, fired = _bridge_with_recorder(monkeypatch)

    # Several "encoding"-class markers in a row collapse to ONE ping.
    bridge.emit(_record("tribev2.main", "Preparing extractor: text"))
    bridge.emit(_record("tribev2.main", "Preparing extractor: audio"))
    bridge.emit(_record("tribev2.main", "Preparing extractor: video"))

    assert fired == ["encoding"]


def test_unrelated_logs_are_ignored(monkeypatch):
    bridge, fired = _bridge_with_recorder(monkeypatch)

    bridge.emit(_record("uvicorn.access", "GET /healthz 200"))
    bridge.emit(_record("hf-space", "accepted job=abc source=s3"))

    assert fired == []


def test_emit_never_raises_on_bad_record(monkeypatch):
    bridge, fired = _bridge_with_recorder(monkeypatch)

    # A record whose getMessage() would raise (args mismatch) must be swallowed —
    # a logging handler that throws would break the inference it's observing.
    bad = logging.LogRecord(
        name="tribev2", level=logging.INFO, pathname=__file__, lineno=0,
        msg="bad %d format", args=("not-an-int",), exc_info=None,
    )
    bridge.emit(bad)  # must not raise
    assert fired == []
