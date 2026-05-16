"""Inference backends — STUB.

Owner: ml-inference worker. See docs/worker-briefs/ml-inference.md.

Build:
  - `MockBackend`: deterministic synthesized activations (no GPU).
  - `TribeBackend`: real TRIBE v2 forward pass (gated on `pip install '.[real]'`).

Both must conform to the `InferenceBackend` protocol below and emit
arrays of shape `(num_timepoints, 20484)` in fp32.
"""

from __future__ import annotations

from typing import Protocol


class InferenceBackend(Protocol):
    model_id: str
    model_version: str

    def infer(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        """Run inference and return an InferenceResult.

        Signature is defined in the worker brief and CONTRACTS.md §4.
        """
        ...


# TODO(ml-inference): implement MockBackend and TribeBackend.
