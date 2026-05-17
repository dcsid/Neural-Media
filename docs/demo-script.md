# Demo script — integration smoke

Deterministic walkthrough for the integration lead to run after a worker
round merges. Hits every shipped feature in priority order; each step
has an explicit pass/fail condition and a recovery action.

Run from repo root with both servers up (see `make dev-api` and
`make dev-web` — the Makefile exports `PYTHONPATH` to survive macOS
sandbox `UF_HIDDEN` on editable `.pth` files).

## Prerequisites

```bash
# Both servers running:
lsof -nP -iTCP:8000 -sTCP:LISTEN   # FastAPI
lsof -nP -iTCP:3000 -sTCP:LISTEN   # Next.js
curl -s http://127.0.0.1:8000/api/v1/health   # → {"status":"ok"}

# All three test suites green:
make test-python
make typecheck-web
```

If any of the above fail, stop and triage. A demo on a sick build is
worse than no demo.

## A. Backend wire-shape checks (CLI; ~30 seconds)

| # | Command | Pass condition |
|---|---|---|
| A1 | `curl -s http://127.0.0.1:8000/api/v1/capabilities` | Returns `{mock, real, real_blockers}`. On this Mac: `real:false`, blockers include at minimum `missing-extra`. |
| A2 | `curl -s http://127.0.0.1:8000/openapi.json \| python3 -c "import json,sys; print(list(json.load(sys.stdin)['components']['schemas']['Body_post_import_api_v1_import_post']['properties'].keys()))"` | Lists `['file', 'mode', 'since', 'until', 'days']`. If only `['file', 'mode']`, the API has stale code in memory — restart. |
| A3 | `curl -s http://127.0.0.1:8000/api/v1/debug` (once Terminal 5's PR lands) | Returns `version, db_path, counts, latest_import, capabilities, disk_usage, uptime_s`. |
| A4 | `curl -s -X POST http://127.0.0.1:8000/api/v1/import -F "file=@/tmp/bogus.exe"` | 400 with `error_code: "file_extension_rejected"` in the body. |
| A5 | `curl -s -X POST http://127.0.0.1:8000/api/v1/import -F "file=@watch_history.txt" -F "since=tomorrow"` | 400 with `error_code: "since_unparseable"`. |

## B. Import flow (browser; ~2 minutes)

Open `http://localhost:3000/import`.

| # | Action | Pass condition |
|---|---|---|
| B1 | Page renders | "Drop your TikTok export to begin." heading; explanatory paragraph mentions `user_data.json`, `Watch History.txt`, and `.zip`. |
| B2 | "Last N" filter present | Number input + unit selector (`minutes`, `hours`, `days`). Default: 14 days. |
| B3 | Mode toggle present | Two radios: Mock (selected), Real. |
| B4 | Real toggle reflects capabilities | Real radio disabled (or has a tooltip / hint) explaining `[real]` extra missing. If Real is selectable on a Mac with no GPU + no extra, that's a Terminal 3 bug. |
| B5 | Stale CLI hint fixed | The "Prefer the CLI?" code snippet shows `python -m neural_media_pipeline ... --mock --days 1`, not the legacy `.importer data/raw/user_data.json`. |
| B6 | Drop the real file | Drag `[redacted-path] Activity/Watch History.txt`. Set "Last 1 hours". Mode Mock. |
| B7 | Status line ticks | Progress shows `parsing → inferring`, `current/total` advances. |
| B8 | Redirect on complete | Page redirects to `/` within ~2s of `complete`. |

## C. Dashboard (browser; ~1 minute)

On `/` after the redirect.

| # | Pass condition |
|---|---|
| C1 | Mock-mode badge visible top-right (or wherever Terminal 3 placed it). Says something like "Mock predictions — synthetic outputs, no video was read." Links to `docs/scientific-framing.md`. |
| C2 | Hero card present: "Across the last {window}, you watched N videos / total H hours. Your most-activated region was {region}..." |
| C3 | Top-region leaderboard: 8 bars, ranked by `by_region[r].mean`, top region highlighted. Hover shows region description. |
| C4 | Hour-of-day histogram: 24 columns, peak callout. |
| C5 | Watched-videos list renders, each row links to `/v/{id}`. |
| C6 | If `by_author` panel is shipped (Terminal 3 sent a proposal that the integration lead applied), the panel renders. Otherwise placeholder text is visible. |

## D. Video detail + brain-viz (browser; ~2 minutes)

Click any video → `/v/{id}`.

| # | Pass condition |
|---|---|
| D1 | Mock-mode badge still visible (same data source). |
| D2 | Per-region readings table: 8 rows × (mean / peak / sustained). Tabular nums, no rounded boxes. |
| D3 | Per-region sparkline timeseries: one mini-chart per region. |
| D4 | Brain mesh renders without `ReactCurrentOwner` console error (R3F v9 fix). |
| D5 | Region legend overlay visible: 8 regions + their swatches + one-line descriptions. |
| D6 | Hover any vertex on the mesh: tooltip shows region name, vertex index, activation value (3 decimals). Unassigned vertices say "(unassigned)" not "undefined". |
| D7 | Camera presets work: clicking Lateral L / Lateral R / Medial L / Medial R / Dorsal / Ventral snaps the camera with ~300ms easing. |
| D8 | Tour mode: click Tour button; camera rotates 360°, scrubber plays through, regions highlight in sequence over ~20s. Cancellable by drag/click/escape. |
| D9 | Region pulse animation: while scrubber is playing, the 8 region surfaces subtly pulse with activation. Stops in DevTools-simulated reduced-motion. |
| D10 | Loading state: refresh the page; wireframe outline visible briefly before the colored surface fades in (not a blank spinner). |

## E. Compare view (browser; ~30 seconds)

Navigate to `/compare`.

| # | Pass condition |
|---|---|
| E1 | Two date pickers (or two preset window choices). |
| E2 | Side-by-side per-region timelines with shared y-axis. |
| E3 | One delta reading per region. |
| E4 | Empty-window second slot renders empty state cleanly (not crash). |

## F. CLI features (terminal; ~3 minutes)

```bash
SMOKE=$(mktemp -d)
mkdir -p "$SMOKE/sqlite" "$SMOKE/videos" "$SMOKE/videos_processed" "$SMOKE/activations"
EXPORT="[redacted-path] Activity/Watch History.txt"
```

| # | Command | Pass condition |
|---|---|---|
| F1 | `python -m neural_media_pipeline --dry-run --minutes 30 "$EXPORT"` | Prints `parsed: N videos, N events; queued: 0; ...` where N matches the past-30-min slice (expect ~15–30 videos). |
| F2 | `python -m neural_media_pipeline --dry-run --hours 1 "$EXPORT"` | Same shape, larger N (~30–60 videos). |
| F3 | `python -m neural_media_pipeline --dry-run --days 1 "$EXPORT"` | ~700 videos. |
| F4 | `python -m neural_media_pipeline --mock --hours 1 --purge-after-inference --data-root "$SMOKE" "$EXPORT"` | Completes in <10s, summary shows `completed: N, failed: 0`. After the run: `find "$SMOKE/videos" -name "*.mp4"` returns nothing (purge worked). `find "$SMOKE/activations" -name "*.npz"` returns N files (activations preserved). |
| F5 | `python -m neural_media_pipeline --mock --hours 1 --purge-after-inference --purge-activations --data-root "$SMOKE" "$EXPORT"` (rerun) | Completes. After: no `.mp4`s AND no `.npz`s. SQLite `region_metrics` table still has rows: `sqlite3 "$SMOKE/sqlite/neural_media.db" "select count(*) from region_metrics;"` returns N×8. |
| F6 | Visit `/v/{any-id-from-F5}` in browser | Mesh renders via PlaceholderMesh fallback (per-vertex NPZ is gone); 8-region bars + sparklines still work. |
| F7 | `python scripts/validate_real_mode.py` (Terminal 2's deliverable) | Runs cleanly on this Mac; reports ✗ on GPU, license, extras with crisp remediation strings. |

Cleanup: `rm -rf "$SMOKE"`.

## G. Failure / retry path

Easiest reproduction: spin up the orchestrator with a guaranteed-to-fail
fetch, then retry against the partial job.

| # | Pass condition |
|---|---|
| G1 | A failed real-mode import (e.g. submitted before `[real]` was installed) appears in the dashboard as a failed run. |
| G2 | `POST /api/v1/import/{failed-id}/retry` returns 200 with a new ImportJob. |
| G3 | `POST /api/v1/import/{complete-id}/retry` returns 409 with `error_code: "job_not_retryable"`. |
| G4 | `POST /api/v1/import/does-not-exist/retry` returns 404 with `error_code: "job_not_found"`. |

## H. Negative space — what should NOT happen

- Real mode submission with `[real]` missing must not silently fall back
  to mock; it must 400 with `error_code` `real_extra_missing`.
- Mock-mode badge must not appear when displayed data is from a
  `model_id` that does NOT start with `tribe-v2-mock`.
- `/api/v1/import` must not accept a `.exe` or arbitrary binary; rejection
  is by extension AND the file content is never executed (no `subprocess`
  on the upload path).
- The pipeline must never log a full source URL (only stable video ids
  derived from the URL hash). `grep -r "https://www.tiktok" data/ logs/`
  should return nothing for any user-watched URL.

## Recovery

- API returns 500 on any endpoint: tail `uvicorn` logs (it's running with
  `--reload` in the foreground or via the background task that's hosting
  it). Most 500s are stale code; restart with
  `make unhide-pth && pkill -f "uvicorn neural_media_api" && make dev-api`.
- Web shows `ReactCurrentOwner` crash on `/v/[id]`: R3F downgrade
  regression. Confirm `cat apps/web/package.json | grep react-three` shows
  `@react-three/fiber ^9` and `@react-three/drei ^10`. If not,
  `cd apps/web && pnpm add @react-three/fiber@^9 @react-three/drei@^10`
  + delete `.next/` + restart.
- `ModuleNotFoundError: No module named 'neural_media_*'` from anywhere:
  `make unhide-pth`.
- Demo-time TikTok export not on this machine: drop
  `data/sample/tiktok_export/user_data.json` or `watch_history.txt`
  instead — both are 8-video fixtures that exercise the full pipeline.
