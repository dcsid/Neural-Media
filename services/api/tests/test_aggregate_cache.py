"""Aggregator memo cache invariants.

The cache is keyed by `(id(store), store.version())`. Behaviour we lock
in:

- Two consecutive calls with the same store + unchanged version return
  the exact same object (cache hit).
- A version bump causes a fresh compute (no stale data).
- Distinct stores never share cache entries.
- The cache is bounded — overflow drops the oldest entry.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from neural_media_api.aggregate import (
    _AGG_CACHE,
    _CACHE_CAP,
    clear_aggregate_cache,
    compute_aggregate,
)
from shared.schemas import AggregateReport, RegionMetrics, VideoMetadata, WatchEvent


class _StubStore:
    """In-memory Store with an externally-controlled version string."""

    def __init__(self) -> None:
        self._version = "v0"
        self.compute_calls = 0
        self._videos: list[VideoMetadata] = [
            VideoMetadata(id="v1", source_url="https://x/1", duration_s=10.0),
        ]
        self._events: list[WatchEvent] = [
            WatchEvent(
                id="we1",
                video_id="v1",
                watched_at=datetime.fromisoformat("2026-05-12T08:00:00+00:00"),
                duration_watched_s=5.0,
            ),
        ]

    def list_videos(self): return self._videos
    def get_video(self, video_id): return next((v for v in self._videos if v.id == video_id), None)
    def get_metrics(self, video_id):
        self.compute_calls += 1
        return [
            RegionMetrics(
                region_id="v1",
                video_id=video_id,
                inference_run_id="run1",
                mean=0.5,
                peak=0.9,
                sustained=0.4,
                timeseries=[0.4, 0.5, 0.6],
            )
        ]
    def get_activation(self, video_id): return None
    def list_watch_events(self): return self._events
    def list_runs(self): return []
    def version(self): return self._version
    def bump(self): self._version = f"v{int(self._version[1:]) + 1}"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_aggregate_cache()


def test_same_version_returns_cached_object() -> None:
    store = _StubStore()
    first = compute_aggregate(store)
    second = compute_aggregate(store)
    assert first is second, "expected cache hit to return the same object"


def test_version_bump_recomputes() -> None:
    store = _StubStore()
    first = compute_aggregate(store)
    store.bump()
    second = compute_aggregate(store)
    assert first is not second
    # Both reports should still be valid AggregateReport instances.
    assert isinstance(second, AggregateReport)


def test_distinct_stores_do_not_share_cache_entries() -> None:
    a, b = _StubStore(), _StubStore()
    ra = compute_aggregate(a)
    rb = compute_aggregate(b)
    # Same shape, but distinct cached objects per store identity.
    assert ra is not rb


def test_cache_is_bounded() -> None:
    """Filling past the cap evicts the oldest entry (FIFO under no rehits)."""
    stores = [_StubStore() for _ in range(_CACHE_CAP + 2)]
    for s in stores:
        compute_aggregate(s)
    assert len(_AGG_CACHE) <= _CACHE_CAP
    # The earliest store's entry is gone; computing again forces a recompute.
    before = stores[0].compute_calls
    compute_aggregate(stores[0])
    assert stores[0].compute_calls > before
