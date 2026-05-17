# Proposal: `AggregateReport.by_author` (CONTRACTS.md §6)

**From:** frontend-dashboard worker
**To:** integration lead → api-orchestrator worker
**Status:** proposal, not yet in `shared/CONTRACTS.md` or `shared/schemas.py`
**Frontend slot:** `apps/web/components/AuthorPlaceholder.tsx` (renders a
"Coming when `AggregateReport.by_author` lands" placeholder today)

## Why

The Wrapped-style dashboard has a clean cadence — hero → ranked regions →
when-you-watch → who-you-watch → brain mesh → watched list. The fourth beat
("who you watch") has no backing data on `AggregateReport` today. Authors are
already on `VideoMetadata` and watch counts are derivable from `WatchEvent`,
but the aggregation lives on the backend so the frontend doesn't need to
fan out N metric calls. Without `by_author` the dashboard renders a dashed
placeholder where its second-most-interesting reveal should go. Proposing
we add it before the demo.

## Proposed shape

Add a new top-level field on `AggregateReport`:

```json
"by_author": [
  {
    "author": "string | null",
    "videos": 0,
    "total_watch_time_s": 0.0,
    "mean_activation": 0.0,
    "top_region": "v1"
  }
]
```

- `author` — the TikTok handle without the leading `@`, exactly as
  `VideoMetadata.author` stores it. `null` reserved for videos where the
  export failed to parse a handle.
- `videos` — distinct watched videos attributed to this author within the
  catalogue (not impression count — collapsing rewatches makes the
  leaderboard interesting rather than rate-dominated).
- `total_watch_time_s` — sum of `duration_watched_s` (falling back to
  `VideoMetadata.duration_s` when null) across rewatches.
- `mean_activation` — average across the author's videos of each video's
  per-region mean averaged across all 8 regions. Same reduction the
  dashboard's "Mean activation" tile already shows, restricted to this
  author's videos. Matches the unitless-comparison framing in
  `docs/scientific-framing.md`.
- `top_region` — the `region_id` (one of `REGION_IDS`) with the highest
  per-author mean. Lets the dashboard render the "FFA-heavy" / "language
  network-heavy" attribution next to each author without a second query.

## Constraints

- **Bounded length.** Cap at the top 20 authors by `videos` to keep the
  response small and prevent long-tail authors from flooding the JSON on
  imports with 10k+ unique creators. The dashboard will render the top 8
  and link to "Show all" if useful later.
- **Stable ordering.** Sort by `videos` desc, then `total_watch_time_s`
  desc, then `author` asc (lexicographic). Deterministic for snapshot
  tests.
- **Mock-mode honesty preserved.** No new model_id semantics. The aggregator
  groups by `VideoMetadata.author`, which is independent of `model_id`.
  The existing `MockModeBadge` already labels mock-derived activations.

## Files to touch

- `shared/CONTRACTS.md` §6 — add `by_author` to the AggregateReport snippet.
- `shared/schemas.py` — `class AuthorBucket`, `AggregateReport.by_author`.
- `shared/types.ts` — `interface AuthorBucket`, append to `AggregateReport`.
- `services/inference/neural_media_inference/aggregate.py` — compute
  per-author rollups during the same pass that builds `by_region`.
- `services/api/tests/test_aggregate.py` — snapshot the new field on the
  reference sample.

## Frontend follow-up (small)

Once the field lands, the placeholder component swaps to a ranked author
list reusing the `RegionLeaderboard` idiom. Already designed; ~120 LOC.
No further contract changes needed on the dashboard side.

## Open question

Should `top_region` use the per-author *mean* across regions (current
proposal) or the *peak* region across that author's videos? Mean is the
more honest summary statistic but peak reads more crisply in the UI
("Creator X spikes V1 hardest"). Happy to defer to whichever the
ml-inference worker thinks pairs better with the existing per-video
metrics.
