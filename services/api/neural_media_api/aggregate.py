"""Aggregator — produces `AggregateReport` from store rows.

Cheap-to-recompute on read (no caching for now). All values are
predicted-activation aggregates only — see docs/scientific-framing.md.
"""

from __future__ import annotations

from .schemas_re_export import (
    AggregateBucket,
    AggregateReport,
    ClusterSummary,
    RegionMetrics,
)
from .store import Store


def _video_mean_activation(metrics: list[RegionMetrics]) -> float:
    """Mean across this video's region means — a single scalar per video."""
    if not metrics:
        return 0.0
    return sum(m.mean for m in metrics) / len(metrics)


def compute_aggregate(store: Store) -> AggregateReport:
    videos = store.list_videos()
    watch_events = store.list_watch_events()

    # Per-video mean activation, used by hour/day rollups.
    video_mean: dict[str, float] = {
        v.id: _video_mean_activation(store.get_metrics(v.id)) for v in videos
    }

    total_videos = len({we.video_id for we in watch_events}) or len(videos)
    total_watch_time_s = sum(
        (we.duration_watched_s or 0.0) for we in watch_events
    )

    sorted_watch = sorted(watch_events, key=lambda we: we.watched_at)
    first_watched_at = sorted_watch[0].watched_at if sorted_watch else None
    last_watched_at = sorted_watch[-1].watched_at if sorted_watch else None

    # by_region: pool every (video, region) metrics row.
    by_region_accum: dict[str, list[RegionMetrics]] = {}
    for v in videos:
        for m in store.get_metrics(v.id):
            by_region_accum.setdefault(m.region_id, []).append(m)

    by_region: dict[str, AggregateBucket] = {
        region_id: AggregateBucket(
            mean=sum(m.mean for m in rows) / len(rows),
            peak=max(m.peak for m in rows),
        )
        for region_id, rows in by_region_accum.items()
    }

    # by_hour_of_day / by_day_of_week: mean predicted activation across all
    # watch events that landed in each bucket. "engagement" here is in the
    # predicted-activation sense only (scientific-framing.md §3).
    hour_sum = [0.0] * 24
    hour_count = [0] * 24
    day_sum = [0.0] * 7
    day_count = [0] * 7
    for we in watch_events:
        score = video_mean.get(we.video_id, 0.0)
        h = we.watched_at.hour
        d = we.watched_at.weekday()  # Mon=0..Sun=6
        hour_sum[h] += score
        hour_count[h] += 1
        day_sum[d] += score
        day_count[d] += 1

    by_hour_of_day = [
        (hour_sum[i] / hour_count[i]) if hour_count[i] else 0.0 for i in range(24)
    ]
    by_day_of_week = [
        (day_sum[i] / day_count[i]) if day_count[i] else 0.0 for i in range(7)
    ]

    # Clusters: not computed yet — the ml-inference worker will own this.
    # Return an empty list rather than a fake cluster so the frontend can
    # show an honest "no clusters yet" state.
    clusters: list[ClusterSummary] = []

    return AggregateReport(
        total_videos=total_videos,
        total_watch_time_s=total_watch_time_s,
        first_watched_at=first_watched_at,
        last_watched_at=last_watched_at,
        by_region=by_region,
        by_hour_of_day=by_hour_of_day,
        by_day_of_week=by_day_of_week,
        clusters=clusters,
    )


__all__ = ["compute_aggregate"]
