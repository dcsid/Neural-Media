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

## Deliverables (current state)

All five landed in rounds 1–3; shipped surface area is described here.

1. **All read endpoints in CONTRACTS.md §7** under `/api/v1/`:
   `/health`, `/videos`, `/videos/{id}`, `/videos/{id}/metrics`,
   `/videos/{id}/activation`, `/regions`, `/aggregate`,
   `/watch-events`, `/inference-runs`.
2. **Write surface**: `POST /api/v1/import` (multipart) +
   `GET /api/v1/import/{id}` (poll). Form fields:
   - `file` — `.json` / `.zip` / `.txt` (extension determines parser)
   - `mode` — `"mock"` (default) or `"real"`
   - `since` — ISO-8601 UTC, half-open lower bound on watch events
   - `until` — ISO-8601 UTC, half-open upper bound
   - `days` — convenience; wins over `since` when both set
   - 400 errors include a machine-readable `error_code` (e.g.
     `real_extra_missing`, `since_unparseable`,
     `file_extension_rejected`) alongside the human-readable `detail`
3. **`POST /api/v1/import/{id}/retry`**: re-drives `run_pending` on
   a partial/failed job under the existing singleton gate. 409 if
   another job is in flight.
4. **`GET /api/v1/capabilities`**: returns
   `{mock: bool, real: bool, real_blockers: string[]}` so the
   frontend can disable the Real toggle before submission. Blocker
   tokens (in priority order): `missing-extra`, `missing-ffmpeg`,
   `missing-yt-dlp`, `missing-gpu`.
5. **`SampleStore`**: reads pre-generated outputs from
   `data/sample/mock_inference/`. Used when `NEURAL_MEDIA_DB_PATH`
   is unset.
6. **`SqliteStore`**: SQLite-backed reader against the catalog DB
   the orchestrator populates. Used when `NEURAL_MEDIA_DB_PATH` is
   set (the demo path).
7. **Aggregator**: computes `AggregateReport` from the store on
   read. Cache exists for hot paths.
8. **Local-only binding**: server binds to `127.0.0.1` only, rejects
   non-loopback `Host` headers. CORS allowlist is the Next.js dev
   origin only.

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

Smoke tests should cover (current suite: 73 tests passing):

- `/api/v1/health` → 200 `{ "status": "ok" }`.
- All `GET` endpoints return 200 with shapes that round-trip through
  the matching `shared.schemas` model.
- `SampleStore` populates from a fixture directory; missing fixture
  yields empty lists (not 500s).
- The server refuses to bind to a non-loopback address.
- `POST /api/v1/import` accepts `.json`/`.zip`/`.txt`; rejects other
  extensions with `error_code=file_extension_rejected`.
- `POST /api/v1/import` with malformed `since` → 400 with
  `error_code=since_unparseable`.
- `POST /api/v1/import` while another job is running → 409 with the
  running `ImportJob` as body (not an error envelope).
- `POST /api/v1/import/{id}/retry` on a complete job → refuses;
  on a partial/failed job → 200 with new job.
- `/api/v1/capabilities` reports correct `real_blockers` when
  prerequisites (extra / ffmpeg / yt-dlp / GPU) are missing.

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
