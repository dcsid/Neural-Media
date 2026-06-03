"""Test harness for the control-plane Lambdas.

boto3 isn't installed in the dev venv (the Lambda runtime provides it), and
``shared/__init__.py`` builds AWS clients at import time. We stub boto3 +
botocore so the modules import cleanly; each test then patches the specific
``shared`` helpers it exercises, so no test ever touches AWS or the network.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

# --- stub boto3 / botocore so `import boto3` in shared/ succeeds ------------
_boto3 = types.ModuleType("boto3")
_boto3.resource = mock.MagicMock(name="boto3.resource")
_boto3.client = mock.MagicMock(name="boto3.client")
sys.modules.setdefault("boto3", _boto3)

_botocore = types.ModuleType("botocore")
_botocore_exc = types.ModuleType("botocore.exceptions")


class _ClientError(Exception):
    """Stand-in for botocore.exceptions.ClientError."""


_botocore_exc.ClientError = _ClientError
_botocore.exceptions = _botocore_exc
sys.modules.setdefault("botocore", _botocore)
sys.modules.setdefault("botocore.exceptions", _botocore_exc)

# --- import root: CodeUri is lambdas/, so `shared`, `jobs_create`, … are
# top-level packages at runtime. Insert at position 0 so this `shared` wins
# over the repo-root `shared/` (schemas) package that `python -m` puts on the
# path via the cwd entry.
_LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
sys.path.insert(0, str(_LAMBDAS))

# --- env vars that shared/* (and the handlers) read at import time ---------
os.environ.setdefault("JOBS_TABLE", "test-jobs")
os.environ.setdefault("RESULTS_BUCKET", "test-results")
os.environ.setdefault("WORKER_FUNCTION_NAME", "test-worker")
os.environ.setdefault("HF_SPACE_URL", "http://hf.test")
os.environ.setdefault("CALLBACK_URL", "http://cb.test/v2/internal/hf-callback")
os.environ.setdefault("HF_CALLBACK_SECRET_SSM_NAME", "/test/secret")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
