# Worker brief — api-orchestrator

## Mission

Own the FastAPI service and the SQLite store. Glue the data-pipeline and
ml-inference outputs into the JSON contract the frontend consumes. Serve
the vertical slice from pre-generated mock outputs first, then back it
with real SQLite + Parquet once the pipeline lands.

## Owned files / directories

- `services/api/**`, specifically:
  - `services/api/neural_media_api/main.py` — app factory + routes.
  - `services/api/neural_media_api/store.py` — `SampleStore`
    (file-backed, for the vertical slice) and later `SqliteStore`.
  - `services/api/neural_media_api/aggregate.py` (you may add this) —
    `AggregateReport` computation over the store.
  - `services/api/tests/**`.
- `services/api/pyproject.toml`.
- `data/sqlite/` (gitignored) — the on-disk DB location.

## Files this worker must NOT touch

- `shared/**`.
- `services/inference/**` (you call into it, you don't modify it).
- `services/pipeline/**`.
- `apps/web/**`.
- `scripts/run-api.sh` is yours to add if you want one; otherwise the
  Makefile target `make dev-api` shells out to uvicorn.

## Deliverables

1. **All endpoints in CONTRACTS.md §7** under `/api/v1/`:
   `/health`, `/videos`, `/videos/{id}`, `/videos/{id}/metrics`,
   `/videos/{id}/activation`, `/regions`, `/aggregate`,
   `/watch-events`, `/inference-runs`.
2. **`SampleStore`**: reads pre-generated outputs from
   `data/sample/mock_inference/` so the vertical slice runs without GPU
   or a pipeline run. Built first.
3. **`SqliteStore`**: SQLite-backed persistence (`videos`,
   `watch_events`, `inference_runs`) plus Parquet/NPZ on disk for
   activations. Replaces `SampleStore` once the pipeline can write
   into it.
4. **Aggregator**: computes the `AggregateReport` shape from the rows
   in the store. Cheap to compute on read; cache later if needed.
5. **Local-only binding**: server binds to `127.0.0.1` only, rejects
   `Host` headers that aren't `localhost` or `127.0.0.1`. CORS allowlist
   is the Next.js dev origin only.

## Interfaces this worker must preserve

- All endpoint paths and JSON shapes from `shared/CONTRACTS.md` §7. The
  frontend's `lib/api.ts` (owned by frontend-dashboard) wraps these.
- `from neural_media_inference import run_inference` — the entry point
  exposed by ml-inference.
- The on-disk format under `data/activations/` (CONTRACTS.md §4).

## How to test the work

```
cd services/api
pip install -e '.[dev]'
pytest -q
```

Smoke tests should cover:

- `/api/v1/health` → 200 `{ "status": "ok" }`.
- All `GET` endpoints return 200 with shapes that round-trip through
  the matching `shared.schemas` model.
- `SampleStore` populates from a fixture directory; missing fixture
  yields empty lists (not 500s).
- The server refuses to bind to a non-loopback address.

## Scientific-framing constraints

- The API description (OpenAPI `description=`) must say "predicted
  average BOLD fMRI response" — never "your brain."
- `/api/v1/aggregate` returns predicted-activation aggregates only.
  No user-engagement score, no addiction estimate, no satisfaction
  inference.

## Out of scope for this worker

- Model code or weights.
- Video download or preprocessing.
- Authentication (the app is single-user / local-only).
- Frontend code.
