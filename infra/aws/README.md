# Neural Media — AWS control plane

AWS infrastructure-as-code (AWS SAM / CloudFormation) for the
single-video "YouTube URL + segment → brain activation" product. The control plane
lives entirely in AWS; the heavy ML work happens on a Hugging Face Space
and reports back via a signed HTTP callback.

**Free-tier sized.** Expected monthly cost at ~1k jobs/month is **$0**.
A CloudWatch billing alarm at **$5/month** is configured as the safety
net — non-negotiable for an unattended student stack.

---

## Architecture

```
[Browser]
   │ POST /v2/jobs   { url }
   ▼
[API Gateway HTTP API]
   │
   ├──► [Lambda jobs_create]    ──► [DynamoDB] ──┐
   │                                              │  async invoke
   │                                              ▼
   ├──► [Lambda jobs_upload]                  [Lambda jobs_worker]
   │     └─► [S3 uploads/]  (presigned PUT)       │
   │                                              │ POST /predict
   │                                              ▼
   │                                       [HF Space  (yt-dlp + TRIBE)]
   │                                              │
   │                                              │ POST /v2/internal/hf-callback
   │                                              │   (X-NM-Token: <secret>)
   │                                              ▼
   ├──► [Lambda jobs_status]   ◄────────────  [Lambda hf_callback]
   │     ▲                                        ├─► [S3 results/{id}.json.gz]
   │     │ GET /v2/jobs/{id}                      └─► [DynamoDB] mark done
[Browser polls]
```

Resource ownership:

| Worker | Owns |
| --- | --- |
| **T2** | The HF Space (POST /predict, the yt-dlp/ffmpeg/TRIBE pipeline, the activation JSON shape). |
| **T3 (this)** | Everything in `infra/aws/`: API Gateway, DynamoDB, S3, the 5 Lambdas. |
| **T4** | The frontend that POSTs `/v2/jobs` and polls `/v2/jobs/{id}`. |

Shared contracts live in the parent brief; if anything in `template.yaml`
contradicts them, fix the template.

---

## Prerequisites

1. AWS account with the AWS CLI configured for region **us-east-1**:
   ```bash
   aws configure
   # Default region: us-east-1
   ```
2. **SAM CLI** installed:
   ```bash
   brew install aws-sam-cli   # macOS
   # or follow https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
   ```
3. The HF Space (worker T2) deployed and reachable; you'll need its base URL.

---

## One-time setup

### 1. Create the shared callback secret

The HF Space signs its callbacks with `X-NM-Token: <secret>`; the
`hf_callback` Lambda verifies it. Generate a secret and store it as an SSM
SecureString:

```bash
SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
aws ssm put-parameter \
    --region us-east-1 \
    --name /neural-media/hf-callback-secret \
    --type SecureString \
    --value "$SECRET"
echo "Hand this to the HF Space deploy: $SECRET"
```

The HF Space needs the same value as a Space secret named `CALLBACK_SHARED_SECRET`
(the env var `services/hf-space/app.py` reads).

### 2. Enable AWS billing metrics (account-wide, one-time)

CloudWatch billing metrics are off by default. Toggle them on in the
AWS console:

> **Billing → Billing preferences → Receive Billing Alerts**

(There is no CLI equivalent that works without root-account credentials.)
Without this, the $5 alarm sits in INSUFFICIENT_DATA forever.

---

## Deploy

First deploy — `--guided` walks the parameter prompts and saves answers
to `samconfig.toml`:

```bash
cd infra/aws
sam build
sam deploy --guided
```

Suggested answers:

| Prompt | Value |
| --- | --- |
| Stack Name | `neural-media-aws` |
| AWS Region | `us-east-1` |
| `Stage` | `dev` |
| `HFSpaceUrl` | URL of the HF Space, e.g. `https://you-yourspace.hf.space` |
| `FrontendOrigin` | Production origin, e.g. `https://your-app.vercel.app` |
| `HFCallbackSecretSsmName` | `/neural-media/hf-callback-secret` |
| `HFCallbackSecretSsmVersion` | `1` (bump on rotation) |
| `BillingAlarmEmail` | `you@example.com` (blank to skip) |
| Confirm changes before deploy | `y` |
| Allow SAM CLI IAM role creation | `y` |

Subsequent deploys: `sam build && sam deploy`.

If you provided `BillingAlarmEmail`, **check your inbox** — AWS sends an
SNS confirmation email; the alarm doesn't notify until you click confirm.

---

## What the template creates

The deploy is a single CloudFormation stack (`neural-media-aws`) holding:

| Logical ID | Type | Notes |
| --- | --- | --- |
| `ResultsBucket` | `AWS::S3::Bucket` — `neural-media-${Stage}-results-${AccountId}` | Block-all-public; AES256 SSE; CORS PUT/GET; lifecycle (`uploads/`=1 day, `results/`=30 days). |
| `JobsTable` | `AWS::DynamoDB::Table` — `neural-media-${Stage}-jobs` | `PAY_PER_REQUEST`; partition key `jobId` (S); TTL on `expiresAt`; SSE on. |
| `HttpApi` | `AWS::Serverless::HttpApi` | HTTP API v2 with CORS (allowed origins: `FrontendOrigin` + `localhost:3000`). |
| `JobsCreateFunction` | `AWS::Serverless::Function` — Python 3.11 / arm64 / 128 MB / 30 s | `POST /v2/jobs`. Validates URL, writes job, async-invokes worker. |
| `JobsUploadFunction` | `AWS::Serverless::Function` — same runtime spec | `POST /v2/jobs/upload` + `POST /v2/jobs/upload/{jobId}/confirm`. Presigned PUT into `uploads/`. |
| `JobsWorkerFunction` | `AWS::Serverless::Function` — same | Async-invoked. POSTs HF Space with callback URL + token. |
| `JobsStatusFunction` | `AWS::Serverless::Function` — same | `GET /v2/jobs/{jobId}`. Reads DynamoDB, presigns `results/` on done. |
| `HfCallbackFunction` | `AWS::Serverless::Function` — same | `POST /v2/internal/hf-callback`. Verifies `X-NM-Token`, writes `results/`, marks done. |
| `JobsCreateFunctionRole` etc. | `AWS::IAM::Role` × 5 | Per-function, least-privilege (see "IAM" below). |
| `BillingAlarmTopic` | `AWS::SNS::Topic` | Notification target for the $5 alarm. |
| `BillingAlarmEmailSubscription` | `AWS::SNS::Subscription` (conditional) | Created only when `BillingAlarmEmail` is non-empty. |
| `BillingAlarm` | `AWS::CloudWatch::Alarm` | `AWS/Billing.EstimatedCharges` > $5 USD, 6-hour period. |

CloudFormation also creates the usual Lambda log groups, Lambda
permissions for HTTP API integration, and the `AWS::Serverless::HttpApi`
implicit `Stage` and `Deployment` resources.

### Stack outputs

After deploy, `aws cloudformation describe-stacks --stack-name neural-media-aws --query 'Stacks[0].Outputs'` returns:

- `ApiEndpoint` — base URL for the frontend; POST `/v2/jobs` here.
- `HfCallbackUrl` — full URL the HF Space POSTs back to.
- `ResultsBucketName` — S3 bucket name.
- `JobsTableName` — DynamoDB table name (matches `JOBS_TABLE` env var).
- `BillingAlarmTopicArn` — SNS topic ARN; subscribe additional endpoints here.

---

## API surface

| Method & path | Lambda | Returns |
| --- | --- | --- |
| `POST /v2/jobs` | `jobs_create` | `201 { jobId }` |
| `POST /v2/jobs/upload` | `jobs_upload` (create variant) | `201 { jobId, uploadUrl, uploadKey, uploadExpiresInSec }` |
| `POST /v2/jobs/upload/{jobId}/confirm` | `jobs_upload` (confirm variant) | `200 { jobId, status }` |
| `GET  /v2/jobs/{jobId}` | `jobs_status` | `200 { jobId, status, createdAt, elapsedSec, resultUrl?, error?, modelVersion? }` or `404` |
| `POST /v2/internal/hf-callback` | `hf_callback` | `200` / `401` (token mismatch) — header `X-NM-Token: <secret>` required |

Job lifecycle values (the contract):

```
status ∈ {
  "pending", "downloading", "inferring", "done",
  "failed_download", "failed_inference", "rejected_duration",
}
```

---

## Environment variables (per Lambda)

Set globally in `template.yaml`:

| Var | Source | Used by |
| --- | --- | --- |
| `JOBS_TABLE` | `Ref JobsTable` | all |
| `RESULTS_BUCKET` | `Ref ResultsBucket` | all |
| `FRONTEND_ORIGIN` | `Ref FrontendOrigin` | all (informational) |
| `HF_SPACE_URL` | `Ref HFSpaceUrl` | jobs_worker |
| `HF_CALLBACK_SECRET_SSM_NAME` | `Ref HFCallbackSecretSsmName` | jobs_worker (signs callbacks), hf_callback (verifies them) — both fetch the secret at runtime via `ssm:GetParameter` |
| `HF_CALLBACK_SECRET_SSM_VERSION` | `Ref HFCallbackSecretSsmVersion` | optional pinned version (empty = latest) |
| `STAGE` | `Ref Stage` | all (informational) |

Per-function additions:

| Function | Extra env |
| --- | --- |
| `jobs_create`, `jobs_upload` | `WORKER_FUNCTION_NAME` (so they can async-invoke the worker) |
| `jobs_worker` | `CALLBACK_URL` (full HTTPS URL the HF Space posts to) |

### Rotating the callback secret

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
aws ssm put-parameter \
  --region us-east-1 \
  --name /neural-media/hf-callback-secret \
  --type SecureString \
  --value "$NEW" \
  --overwrite
# Note the new Version returned. Then:
sam deploy --parameter-overrides HFCallbackSecretSsmVersion=<new-version>
```

Update the HF Space secret to match in the same window — there is no
overlap period built in.

---

## IAM (least-privilege, per Lambda)

| Lambda | DynamoDB | S3 | Lambda |
| --- | --- | --- | --- |
| `jobs_create` | RW on `JobsTable` | — | `Invoke` on `jobs_worker` |
| `jobs_upload` | RW on `JobsTable` | `PutObject` on `uploads/*` | `Invoke` on `jobs_worker` |
| `jobs_worker` | RW on `JobsTable` | `GetObject` on `uploads/*` (for presigning) | — |
| `jobs_status` | Read on `JobsTable` | `GetObject` on `results/*` (for presigning) | — |
| `hf_callback` | RW on `JobsTable` | `PutObject` on `results/*` | — |

All Lambdas also have the AWS-managed `AWSLambdaBasicExecutionRole` for
CloudWatch Logs (added automatically by SAM).

---

## Expected cost at ~1k requests/month

| Service | Free-tier coverage | Estimated $ |
| --- | --- | --- |
| Lambda invocations + duration | 1M req + 400k GB-s | $0 |
| API Gateway HTTP API | 1M req/mo (free tier first 12 months, $1/M after) | $0 |
| DynamoDB on-demand | 25 GB storage + plenty of RCU/WCU for 1k jobs | $0 |
| S3 | 5 GB + 20k GET + 2k PUT | $0 |
| CloudWatch (logs + 1 alarm + 1 metric) | 10 metrics + 5 GB logs + 10 alarms | $0 |
| SNS (1 topic, ≤1k publishes) | 1M publishes | $0 |
| **Total** | | **$0** |

The 30-day TTL on DynamoDB rows + 30-day S3 lifecycle on `results/` keep
storage from drifting upward over time.

---

## Local invocation

`sam local start-api` runs the HTTP API on `http://localhost:3000` with
Docker-hosted Lambda runtimes:

```bash
sam local start-api \
  --parameter-overrides 'HFSpaceUrl="http://host.docker.internal:8080" FrontendOrigin="http://localhost:3000" HFCallbackSecretSsmName="/neural-media/hf-callback-secret" HFCallbackSecretSsmVersion="1"'
```

`sam local invoke <FunctionLogicalId> -e events/<event>.json` runs one
Lambda against a hand-crafted event payload.

---

## Teardown

```bash
# S3 must be emptied before stack deletion:
aws s3 rm "s3://$(aws cloudformation describe-stacks \
  --stack-name neural-media-aws \
  --query 'Stacks[0].Outputs[?OutputKey==`ResultsBucketName`].OutputValue' \
  --output text)" --recursive

sam delete --stack-name neural-media-aws
```

CloudWatch log groups and the SSM SecureString are not removed by
`sam delete`. Drop them manually for a clean slate:

```bash
aws logs delete-log-group --log-group-name /aws/lambda/neural-media-dev-jobs-create
# ...repeat per function
aws ssm delete-parameter --name /neural-media/hf-callback-secret
```

---

## Security trade-offs (documented)

1. **The callback secret is fetched at runtime, never stored in the Lambda
   env.** The Lambda env carries only the SSM parameter *name* + version
   (`HF_CALLBACK_SECRET_SSM_NAME`/`_VERSION`); `shared.get_callback_secret()`
   calls `ssm:GetParameter` with `WithDecryption=True` on first use and
   memoises it for the warm container. The cleartext never appears in
   `lambda:GetFunctionConfiguration` output. (CFN's `{{resolve:ssm-secure}}`
   dynamic reference is deliberately NOT used — it would bake the cleartext
   into the function env.)
2. **No WAF or rate-limiting.** API Gateway HTTP API doesn't support WAF
   directly (REST API does). For a public-facing demo at $0, rely on the
   $5 billing alarm + DynamoDB on-demand cost ceiling; add per-IP
   throttling at the API Gateway stage if scraping becomes an issue.
3. **Job rows are world-readable to anyone with a `jobId`.** `jobId` is
   a 128-bit uuid4 hex, so guessing is infeasible, but the URL is the
   capability — share it with the same care as the resulting brain.
