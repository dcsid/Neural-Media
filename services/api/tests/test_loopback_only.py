"""Loopback-only invariants.

Two things must be true regardless of how the API is launched:

  1. Any HTTP request whose `Host` header is not `localhost` / `127.0.0.1`
     is rejected with a 400 — defense against DNS rebinding from a
     browser running on the same machine.
  2. The dev runner that the Makefile shells out to binds only to
     `127.0.0.1`. We assert that at the source level (the
     `if __name__ == "__main__"` block in main.py) rather than trying to
     fight uvicorn at the socket layer.
"""

from __future__ import annotations

import ast
from pathlib import Path

from fastapi.testclient import TestClient
from neural_media_api.main import create_app


def test_host_header_must_be_loopback() -> None:
    # base_url=loopback so the *default* request would pass; we override the
    # Host header per-request to prove non-loopback values are rejected.
    client = TestClient(create_app(), base_url="http://localhost")
    r = client.get("/api/v1/health", headers={"host": "evil.example.com"})
    assert r.status_code == 400
    assert "host" in r.json()["detail"].lower()


def test_host_header_localhost_with_port_allowed() -> None:
    client = TestClient(create_app(), base_url="http://localhost")
    r = client.get("/api/v1/health", headers={"host": "localhost:8000"})
    assert r.status_code == 200
    r = client.get("/api/v1/health", headers={"host": "127.0.0.1:8000"})
    assert r.status_code == 200


def test_default_test_client_host_testserver_is_rejected() -> None:
    """TestClient's default Host is `testserver`. Prove the guard catches it."""
    client = TestClient(create_app())  # default base_url -> Host: testserver
    r = client.get("/api/v1/health")
    assert r.status_code == 400


def test_empty_host_header_is_rejected() -> None:
    """A missing/empty Host must be refused, not silently allowed.

    HTTP/1.1 clients always send a Host; an empty one is anomalous, and a
    DNS-rebinding probe could omit it to slip past an allowlist that only
    screens known-bad non-empty values. The guard returns 400.
    """
    client = TestClient(create_app(), base_url="http://localhost")
    r = client.get("/api/v1/health", headers={"host": ""})
    assert r.status_code == 400
    assert "host" in r.json()["detail"].lower()


def test_dev_runner_binds_loopback_only() -> None:
    """Source-level invariant: uvicorn.run is called with host='127.0.0.1'."""
    main_path = Path(__file__).resolve().parents[1] / "neural_media_api" / "main.py"
    tree = ast.parse(main_path.read_text())

    uvicorn_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
    ]
    assert uvicorn_calls, "expected a `uvicorn.run(...)` call in main.py"

    for call in uvicorn_calls:
        host_kw = next((kw for kw in call.keywords if kw.arg == "host"), None)
        assert host_kw is not None, "uvicorn.run(...) missing `host=` kwarg"
        assert isinstance(host_kw.value, ast.Constant)
        assert host_kw.value.value == "127.0.0.1", (
            "dev runner must bind to 127.0.0.1 only"
        )
