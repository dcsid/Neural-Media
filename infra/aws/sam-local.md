# `sam local` for the single-video pipeline

This doc is the local-development companion to `docs/single-video-deploy.md`.
It explains how to run T3's API Gateway + Lambda template on your laptop,
pointed at the mock HF Space (`services/hf-space/mock_local.py`) instead
of the real one, with no AWS spend.

T3 owns the actual SAM template at `infra/aws/template.yaml`. The commands
below assume that file exists and exposes the parameters the brief lists
(`HfSpaceUrl`, `CallbackSecret`, `ResultsBucket`, `JobsTable`). If T3
renames a parameter, the value of the env var changes — the structure
of this doc does not.

---

## TL;DR

Three terminals:

```bash
# T1 — mock HF Space
python services/hf-space/mock_local.py

# T2 — DynamoDB local
docker run --rm -p 8000:8000 amazon/dynamodb-local

# T3 — SAM local API
cd infra/aws
sam build
sam local start-api --port 3001 \
  --env-vars sam-env.json \
  --docker-network host
```

Then smoke-test:

```bash
API_BASE=http://127.0.0.1:3001 make smoke-single
```

The rest of this doc explains each piece and the workarounds for the
sharp edges (Docker networking on macOS, presigned-URL signing against
moto, etc.).

---

## Prereqs

| tool                   | install                                                         |
|------------------------|-----------------------------------------------------------------|
| Docker Desktop ≥ 4.20  | https://www.docker.com/products/docker-desktop/                  |
| SAM CLI ≥ 1.115        | `brew install aws/tap/aws-sam-cli`                              |
| Python 3.11+           | already required by the rest of the repo                        |
| AWS CLI v2             | `brew install awscli` (only needed for `aws dynamodb` helpers)  |

`sam local start-api` runs each Lambda inside a Docker container, so
Docker Desktop must be running. SAM downloads `public.ecr.aws/sam/...`
images on first invocation — count on ~500 MB the first time.

You do **not** need real AWS credentials for `sam local`. The Lambda
containers see fake `AWS_ACCESS_KEY_ID=testing` values; they only matter
when the code calls a real AWS API. The notes below explain how we keep
DynamoDB and S3 calls pointed at local stand-ins.

---

## 1. Mock HF Space (port 8001)

```bash
python3 -m pip install -r services/hf-space/requirements-mock.txt
python services/hf-space/mock_local.py
```

The mock returns 202 immediately and POSTs back to `callbackUrl` 5-15s
later. 1-in-8 calls return `failed_download` / `tiktok_blocked` so the
frontend error path gets exercised.

**Docker networking**: when SAM Lambdas (running inside Docker) call the
mock (running on the host), they cannot use `http://127.0.0.1:8001` —
that points at the Lambda container itself. Use one of:

- macOS / Windows: `http://host.docker.internal:8001`
- Linux: `--docker-network host` and `http://127.0.0.1:8001`

The `--parameter-overrides` example below uses
`host.docker.internal:8001` because that works on the most platforms.

---

## 2. DynamoDB local (port 8000)

```bash
docker run --rm -p 8000:8000 amazon/dynamodb-local
```

DynamoDB Local is a single-binary in-memory clone of DynamoDB. It honours
the same API surface (PutItem, UpdateItem, Query, etc.), persists nothing
between restarts, and listens on `:8000` by default.

**Create the `jobs` table once per restart**:

```bash
aws dynamodb create-table \
  --endpoint-url http://localhost:8000 \
  --region us-east-1 \
  --table-name jobs \
  --attribute-definitions AttributeName=jobId,AttributeType=S \
  --key-schema AttributeName=jobId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

(If T3's `template.yaml` declares the `AWS::DynamoDB::Table` resource,
`sam local generate-event` won't create it in DynamoDB Local — the
template is for CloudFormation, not for local. Always run the create
command above by hand after starting the container.)

The Lambdas need to know to talk to local instead of real DynamoDB:

```bash
export DYNAMODB_ENDPOINT_URL=http://host.docker.internal:8000   # passed in sam-env.json
```

T3's Lambda code should consult `DYNAMODB_ENDPOINT_URL` and, if set,
build the boto3 client with `endpoint_url=...`. If T3's code doesn't do
that, ask T3 to add it — it's a 3-line change and there is no clean way
to redirect boto3 client traffic from outside the process.

---

## 3. S3: real bucket vs. moto

Two options. Pick one — both work; choose by which trade-off bites less.

### Option A — real S3 bucket with limited perms (recommended)

Faster path because presigned URLs Just Work — boto3 signs them against
the real endpoint and the browser can fetch them directly.

```bash
aws s3 mb s3://neural-media-dev-results-${USER}
aws s3api put-public-access-block \
  --bucket neural-media-dev-results-${USER} \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Configure a 1-day lifecycle expiry on the bucket so test runs don't
accumulate. Total cost for the demo: pennies per month.

Set `ResultsBucket=neural-media-dev-results-${USER}` in `sam-env.json`.
The Lambdas use your local `~/.aws/credentials` to PutObject and
generate_presigned_url — no IAM role required because `sam local` runs
on your machine.

### Option B — moto-server (no AWS account needed)

```bash
python3 -m pip install moto[server]
moto_server --port 5000 s3
```

Pros: no AWS account, no spend. Cons: presigned URLs moto generates
won't work against the real S3 endpoint, and the browser can't fetch
from `http://localhost:5000` from a deployed frontend (it works for
sam-local + localhost web, which is the only context where Option B
is viable).

To use moto, set in `sam-env.json`:

```jsonc
{
  "JobsCreate":    { "S3_ENDPOINT_URL": "http://host.docker.internal:5000" },
  "HfCallback":    { "S3_ENDPOINT_URL": "http://host.docker.internal:5000" },
  "JobsStatus":    { "S3_ENDPOINT_URL": "http://host.docker.internal:5000" }
}
```

T3's S3 client code needs the same `endpoint_url` plumbing as the
DynamoDB local section above.

---

## 4. `sam-env.json` — env vars for each Lambda

Create `infra/aws/sam-env.json` (gitignored — it lists per-user values):

```jsonc
{
  "JobsCreate": {
    "JOBS_TABLE": "jobs",
    "DYNAMODB_ENDPOINT_URL": "http://host.docker.internal:8000",
    "HF_SPACE_URL": "http://host.docker.internal:8001/predict",
    "CALLBACK_BASE_URL": "http://host.docker.internal:3001/v2/internal/hf-callback",
    "CALLBACK_SECRET": "dev-mock"
  },
  "JobsStatus": {
    "JOBS_TABLE": "jobs",
    "DYNAMODB_ENDPOINT_URL": "http://host.docker.internal:8000",
    "RESULTS_BUCKET": "neural-media-dev-results-${USER}"
  },
  "HfCallback": {
    "JOBS_TABLE": "jobs",
    "DYNAMODB_ENDPOINT_URL": "http://host.docker.internal:8000",
    "RESULTS_BUCKET": "neural-media-dev-results-${USER}",
    "CALLBACK_SECRET": "dev-mock"
  }
}
```

The keys (`JobsCreate`, etc.) must match the `LogicalId` of each Function
resource in `template.yaml`. Ask T3 if the names don't match — those are
T3's to set.

---

## 5. Run it

```bash
cd infra/aws
sam build
sam local start-api --port 3001 --env-vars sam-env.json
```

In a fourth terminal, run the frontend pointed at sam local:

```bash
NEXT_PUBLIC_API_BASE_V2=http://127.0.0.1:3001 pnpm --filter @neural-media/web dev
```

And smoke-test the whole chain:

```bash
API_BASE=http://127.0.0.1:3001 make smoke-single
```

The smoke test creates a job, polls until terminal, and validates the
returned `byRegion` payload has all eight regions. Expected wall time
end-to-end: ~10-20 seconds (mock infer is 5-15s + a few seconds of
polling).

---

## Sharp edges

**`sam local` doesn't run async invocations.** The real architecture has
`jobs_create` async-invoke `jobs_worker`. SAM local Lambdas are
request/response only. T3 should either fold the worker into
`jobs_create` for the local path, or call the HF Space directly from
`jobs_create` (skipping a `jobs_worker` Lambda) — and let async invoke
only happen in deployed mode. Either approach makes `sam local` work
without surprises.

**Callback URL changes when you re-run.** `sam local` doesn't pin port
internally — if you set `--port 3001` it'll bind there, but every
fresh `sam local start-api` invocation gets a fresh container set, so
existing in-flight callbacks from a previous run will land on a server
that's gone. Just re-create the job.

**DynamoDB local is volatile.** Restarting the container drops the
table. Re-run the `aws dynamodb create-table` command from §2.

**JVM startup.** DynamoDB Local takes ~3-5 seconds to be ready after
the container starts. Smoke tests against an empty boot will fail on
the first poll — wait or add a `aws dynamodb wait` step.

---

## See also

- `docs/single-video-deploy.md` — production deployment runbook.
- `services/hf-space/mock_local.py` — the mock the Lambdas talk to in
  this dev loop.
- `make help-single` — the discoverable surface for the v2 targets.
