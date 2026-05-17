"""Aggregator — produces `AggregateReport` from store rows.

All values are predicted-activation aggregates only — see
docs/scientific-framing.md. The hot path scans every (video, region)
metrics row plus every watch event, which gets expensive once a store
holds >1000 videos. We memoize by `(id(store), store.version())` so
identical inputs hit the cached report; any new run bumps the store's
version and invalidates automatically.
"""

from __future__ import annotations

from collections import OrderedDict

from .schemas_re_export import (
    REGION_IDS,
    AggregateBucket,
    AggregateReport,
    AuthorBucket,
    ClusterSummary,
    RegionMetrics,
)
from .store import Store

# Cap on the by_author leaderboard. The dashboard renders the top 8 by
# default; 20 lets the future "show all" affordance stay client-side
# without a second request, while keeping JSON payloads small even on
# 10k+ unique-creator histories.
_AUTHOR_TOP_N = 20


def _video_mean_activation(metrics: list[RegionMetrics]) -> float:
    """Mean across this video's region means — a single scalar per video."""
    if not metrics:
        return 0.0
    return sum(m.mean for m in metrics) / len(metrics)


# Tiny LRU. A single user usually has one store instance, so this rarely
# holds more than one entry — the cap exists to bound memory when tests or
# multi-tenant deployments hand in different store instances.
_CACHE_CAP = 8
_AGG_CACHE: "OrderedDict[tuple[int, str], AggregateReport]" = OrderedDict()


def _cache_get(key: tuple[int, str]) -> AggregateReport | None:
    hit = _AGG_CACHE.get(key)
    if hit is not None:
        _AGG_CACHE.move_to_end(key)
    return hit


def _cache_put(key: tuple[int, str], report: AggregateReport) -> None:
    _AGG_CACHE[key] = report
    _AGG_CACHE.move_to_end(key)
    while len(_AGG_CACHE) > _CACHE_CAP:
        _AGG_CACHE.popitem(last=False)


def clear_aggregate_cache() -> None:
    """Drop every memoized aggregate. Used by tests; rarely needed in prod."""
    _AGG_CACHE.clear()


def compute_aggregate(store: Store) -> AggregateReport:
    key = (id(store), store.version())
    hit = _cache_get(key)
    if hit is not None:
        return hit
    report = _compute_aggregate(store)
    _cache_put(key, report)
    return report


def _compute_aggregate(store: Store) -> AggregateReport:
    videos = store.list_videos()
    watch_events = store.list_watch_events()

    # Per-video mean activation, used by hour/day rollups.
    video_mean: dict[str, float] = {
        v.id: _video_mean_activation(store.get_metrics(v.id)) for v in videos
    }

    total_videos = len({we.video_id for we in watch_events}) or len(videos)

    # TikTok's export almost never includes per-event watch durations
    # (the `duration_watched_s` column is null for the vast majority of
    # rows). Fall back to the underlying video's `duration_s` so the
    # dashboard's "watch time" stat reflects a meaningful upper bound
    # ("if you watched each video to completion") instead of a flat 0.
    video_duration: dict[str, float] = {v.id: v.duration_s for v in videos}
    total_watch_time_s = sum(
        we.duration_watched_s
        if we.duration_watched_s is not None
        else video_duration.get(we.video_id, 0.0)
        for we in watch_events
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

    # by_author: per-creator rollup capped at top-20. See CONTRACTS.md §6
    # and docs/worker-briefs/aggregate-by-author-proposal.md.
    #
    # videos:           distinct videos attributed to this author. Rewatches
    #                   collapse into one row — the leaderboard tracks who
    #                   shows up in the catalog, not impression rate.
    # total_watch_time_s: sums duration_watched_s per rewatch, falling back
    #                   to VideoMetadata.duration_s when null (the export
    #                   rarely carries the per-event field).
    # mean_activation:  average across the author's videos of each video's
    #                   per-region mean averaged across all 8 regions.
    # top_region:       region with the highest per-author PEAK across that
    #                   author's videos (comparative claim — matches
    #                   docs/scientific-framing.md).
    #
    # author=None covers videos whose URL didn't carry an @handle (e.g.
    # tiktokv.com/share/video/<id>/ share-shortlinks). Surfaced as a real
    # row so the user can see the bucket size; suppressed from the
    # frontend's leaderboard view if the placeholder UX prefers.
    rewatch_count: dict[str | None, int] = {}  # noqa: F841 (tracks rewatches only)
    author_videos: dict[str | None, set[str]] = {}
    author_watch_s: dict[str | None, float] = {}
    author_video_means: dict[str | None, list[float]] = {}
    author_region_peaks: dict[str | None, dict[str, float]] = {}

    video_by_id = {v.id: v for v in videos}
    for we in watch_events:
        v = video_by_id.get(we.video_id)
        if v is None:
            continue
        author = v.author  # may be None
        author_videos.setdefault(author, set()).add(v.id)
        author_watch_s[author] = author_watch_s.get(author, 0.0) + (
            we.duration_watched_s
            if we.duration_watched_s is not None
            else video_duration.get(we.video_id, 0.0)
        )

    for author, vid_ids in author_videos.items():
        means: list[float] = []
        region_peaks: dict[str, float] = {}
        for vid_id in vid_ids:
            mrows = store.get_metrics(vid_id)
            if mrows:
                means.append(_video_mean_activation(mrows))
                for m in mrows:
                    prev = region_peaks.get(m.region_id, float("-inf"))
                    if m.peak > prev:
                        region_peaks[m.region_id] = m.peak
        author_video_means[author] = means
        author_region_peaks[author] = region_peaks

    by_author: list[AuthorBucket] = []
    for author, vid_ids in author_videos.items():
        means = author_video_means.get(author, [])
        mean_activation = sum(means) / len(means) if means else 0.0
        peaks = author_region_peaks.get(author, {})
        # Prefer a region the catalog actually has metrics for; fall back
        # to the first canonical region if this author's videos have no
        # metrics yet (an inference run could still be pending).
        top_region = (
            max(peaks.items(), key=lambda kv: kv[1])[0]
            if peaks
            else REGION_IDS[0]
        )
        by_author.append(
            AuthorBucket(
                author=author,
                videos=len(vid_ids),
                total_watch_time_s=author_watch_s.get(author, 0.0),
                mean_activation=mean_activation,
                top_region=top_region,
            )
        )

    # videos desc, then total_watch_time_s desc, then author asc.
    # `author=None` sorts after all string handles (None → empty string for
    # the tertiary key) so the leaderboard prefers attributable rows.
    by_author.sort(
        key=lambda a: (-a.videos, -a.total_watch_time_s, a.author or "~")
    )
    by_author = by_author[:_AUTHOR_TOP_N]

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
        by_author=by_author,
        clusters=clusters,
    )


__all__ = ["clear_aggregate_cache", "compute_aggregate"]
