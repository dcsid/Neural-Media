# Continuous integration

One workflow lives here: `ci.yml`. It runs on every push to `main` and
every PR targeting `main`. Four jobs run in parallel; any single failure
fails the workflow and (when branch protection is enabled) blocks the
merge.

| Job        | Wall time | What it catches                                    |
|------------|-----------|----------------------------------------------------|
| `frontend` | ~3-5 min  | TypeScript errors, Next.js prod-build regressions  |
| `python`   | ~2-3 min  | Lambda + services syntax errors, pytest failures   |
| `cfn`      | ~2-3 min  | SAM template drift — cfn-lint + sam build          |
| `e2e`      | ~5-8 min  | Playwright regressions on `/single` user journey   |

Free-tier budget: GitHub Actions gives public repos unlimited runtime,
and the standard student account ~2,000 private-repo minutes/month.
Each full run is ~15 wall-clock minutes (parallel) but consumes ~18-22
billed minutes (sum across jobs). At one merge a day this lives well
inside the free tier; concurrency cancellation drops superseded runs to
keep the budget honest during rebase storms.

---

## What each job would have caught

The four jobs map onto distinct failure classes — most of them tracked
back to specific patches that landed in the last few weeks. If this
workflow had been live, each of those patches would have arrived as a
red CI run instead of a follow-up commit.

### `frontend` — typecheck + prod build

Runs `pnpm --filter @neural-media/web typecheck` and then `pnpm
--filter @neural-media/web build`. Two distinct failure modes:

- **Contract drift between `shared/types.ts` and the React code.**
  When the backend grows a status enum or renames a region, the
  frontend imports break and `tsc --noEmit` fails before any runtime.
  Worth flagging because Next's dev server happily papers over many
  type errors via lazy compilation — the typecheck is the only place
  these consistently surface.

- **Production-only Next.js errors.** `next build` runs the full
  page-collection + RSC bundling pipeline that `next dev` skips.
  Hydration mismatches, missing dynamic exports, accidental Node-only
  imports in client components all blow up here. The `/single` page
  in particular went through several `feat(web)` and `fix(brain-viz)`
  passes — a prod-build gate would have caught the StrictMode remount
  bug fixed in `d4ce6f6` before it shipped to a reviewer.

### `python` — compile + pytest

Two phases:

1. `python -m compileall` over every Lambda handler and every
   `services/*/neural_media_*/` package. This is a near-instant
   syntax-only check, but Lambdas are the place it matters most:
   a `SyntaxError` doesn't appear until cold start, where it
   surfaces as an opaque 502 from API Gateway with the real
   traceback hidden in CloudWatch. The compile step makes the
   error obvious at PR review.

2. `pip install -e services/{inference,pipeline,api}` (mock backend
   only — `[real]` extras need TRIBE + torch and are too big for
   CI) followed by `pytest -q` in each service.

   The pytest gate would have failed on commits like `e92ac39`
   *("fix(infra/aws): match HF Space /predict body to its pydantic
   schema")* and `a302dd2` *("fix(hf-space-mock): align /predict
   request + callback to canonical contract")* — both were
   contract-drift fixes that landed reactively, after the mismatch
   broke an integration run. A pytest covering the request schema
   would have flagged the drift on the originating PR.

### `cfn` — SAM template lint + build

`sam validate --lint` runs cfn-lint against `infra/aws/template.yaml`.
`sam build` packages the Lambda artefacts. Neither contacts AWS or
needs credentials.

The audit run on `audit/local-dry-runs` (T5, prior round) found four
cfn-lint findings in the current `template.yaml`. The `cfn` job would
have caught all of them on the original `feat(infra)` PR:

| ID    | Issue                                                                                             |
|-------|---------------------------------------------------------------------------------------------------|
| E3004 | Circular dependency: `HttpApi ↔ JobsUploadFunction ↔ JobsWorkerFunction`. CloudFormation rejects. |
| E1027 | `{{resolve:ssm-secure:…}}` used in `Globals.Function.Environment.Variables` — unsupported.        |
| E3002 | `MaxAgeSeconds: 3600` on S3 `CorsRule` — the property is `MaxAge`.                                |
| E1029 | `${HFSpaceUrl}` in a Parameter `Description` text without `Fn::Sub`. Cosmetic.                    |

**Heads-up to the coordinator:** `cfn` is **expected to be red** on
the first run of this branch and stays red until
`fix/aws-template-blockers` (or whatever T3 names the follow-up)
lands. That's the point of having the gate in CI — every later PR
sees the failure and can't pretend it isn't there.

### `e2e` — Playwright regression net

Boots the mock backend on :3001 + the real Next.js dev server on
:3000 (via `playwright.config.ts`'s `webServer:` block) and runs the
four scenarios T4 + T6 wrote in `c8d385e`:

- happy path: URL → activation JSON → brain renders
- BLOCK_ME: `failed_download` → upload fallback CTA appears
- LONG_VIDEO: `rejected_duration` → terminal error message
- polling-cancel on unmount: no zombie GET requests after navigate-away

A green run on every PR is the strongest signal we have that the
end-to-end vertical hasn't drifted. On failure, the Playwright HTML
report is uploaded as a `playwright-report` artifact (7-day retention)
so the reviewer can scrub through the trace timeline directly.

---

## Local pre-flight

Before pushing, the same checks roughly correspond to:

```bash
# frontend
pnpm install --frozen-lockfile
pnpm --filter @neural-media/web typecheck
NEXT_PUBLIC_API_BASE_V2=http://localhost:3001 pnpm --filter @neural-media/web build

# python
python -m compileall -q infra/aws/lambdas services/*/neural_media_*/
for svc in services/inference services/pipeline services/api; do
  ( cd "$svc" && python -m pytest -q )
done

# cfn
( cd infra/aws && sam validate --lint && sam build )

# e2e
pnpm -C tests/e2e exec playwright install --with-deps chromium
make e2e
```

`make e2e` is the same target CI calls.

---

## Running the workflow locally with `act`

`act` (https://github.com/nektos/act) emulates GitHub Actions inside
Docker. Useful for iterating on `ci.yml` itself without burning
remote CI minutes:

```bash
brew install act
# Run the cfn job in isolation:
act -W .github/workflows/ci.yml -j cfn --container-architecture linux/amd64
```

Known limitations:

- `pnpm/action-setup@v4` works under `act` but downloads its own
  pnpm binary each run (no caching across `act` invocations).
- `actions/setup-node@v4`'s `cache: pnpm` requires
  `~/.cache/pnpm-store`, which `act` mounts read-write — works on
  macOS + Linux, may need `--bind` on Windows hosts.
- `actions/upload-artifact@v4` is a no-op under `act` (no remote
  artifact store). The `e2e` job's `--with-deps` apt install runs
  but is wasted disk inside the throwaway container — pass
  `-j frontend` or `-j cfn` to avoid that overhead while iterating.
- `act` images are larger (~1 GB Ubuntu vs. ~30 MB minimal). The
  default `medium` image is fine; the `large` image only matters
  if a job needs `docker` inside.

If `act` misbehaves, just push the branch — the real Actions runner
is the source of truth.

---

## Triggering manually

Coordinator → Actions tab → `ci` workflow → "Run workflow" → pick a
branch. The `workflow_dispatch` trigger at the top of `ci.yml` makes
this work without touching code or merging anything.

---

## When CI is failing for non-code reasons

- **`pnpm install` timeouts on the public registry**: the cache key
  is the lockfile's hash, so an unchanged lockfile hits the warm
  cache; cold-cache pulls happen on first run after any dep bump.
  Retry the workflow.
- **SAM CLI install fails to fetch the latest release**: the URL is
  pinned to `releases/latest`, which is occasionally throttled.
  Re-running the job usually clears it. If it persists, pin the
  version inside `ci.yml` to a specific release tag.
- **Playwright apt install hangs**: rare; usually a stuck Ubuntu
  mirror. Re-run.

Workflow re-runs cost the same as the first run, so don't be shy.
