# Worker briefs

Five briefs, five workers, five branches.

| Brief                                          | Branch                       | Owned directory                              |
|------------------------------------------------|------------------------------|----------------------------------------------|
| [ml-inference](./ml-inference.md)              | `worker/ml-inference`        | `services/inference/`                        |
| [data-pipeline](./data-pipeline.md)            | `worker/data-pipeline`       | `services/pipeline/` (worker creates this)   |
| [api-orchestrator](./api-orchestrator.md)      | `worker/api-orchestrator`    | `services/api/`                              |
| [frontend-dashboard](./frontend-dashboard.md)  | `worker/frontend-dashboard`  | `apps/web/app/`, `apps/web/components/` (non-brain), `apps/web/lib/` |
| [brain-viz](./brain-viz.md)                    | `worker/brain-viz`           | `apps/web/components/brain/`, `apps/web/public/brain/` |

## Coordination rules

1. **`shared/` is sacred.** Any change to `shared/CONTRACTS.md`,
   `shared/schemas.py`, or `shared/types.ts` is a coordinated PR via the
   integration lead — never edited from a worker branch alone.
2. **No cross-worker file edits.** If you need a peer worker to expose
   something, ask the integration lead to update the contract first.
3. **Tests in your own directory.** Workers should not add tests under
   `tests/` at the repo root — each service / app has its own.
4. **Branch hygiene.** Rebase onto `main` daily. Open a PR back to
   `main` when your brief's deliverables are met.

The vertical-slice merge order, once all five briefs have something to
ship:

1. `ml-inference` (mock backend + sample-output build script).
2. `api-orchestrator` (SampleStore-backed endpoints).
3. `frontend-dashboard` (renders against the API).
4. `brain-viz` (drops into the slot the dashboard exposes).
5. `data-pipeline` (replaces the sample data with real downloads).
