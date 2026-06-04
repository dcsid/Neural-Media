# Single-Video Pipeline — Deployment Runbook

From-scratch deploy of the **v2 single-video architecture**: a user pastes a
**YouTube URL** and picks a **≤90-second segment**; an AWS API Gateway routes
the request through a Lambda chain to a HuggingFace Space that runs TRIBE on
just that window; results land in S3 + DynamoDB and surface back in the browser.

**You do not need to know AWS to follow this.** Every step is a copy-pasteable
command. Do them **in order** — later steps consume values produced by earlier
ones. If a step fails, its troubleshooting block usually has the fix.

```
[Browser /single page]
     │  POST /v2/jobs { url, startSec, endSec }
     ▼
[API Gateway HTTP API] ─► [Lambda jobs_create] ─► [DynamoDB jobs table]
                                   │ async invoke
                                   ▼
                           [Lambda jobs_worker] ─► [HF Space POST /predict]
                                                        │  yt-dlp --download-sections
                                                        │  ffmpeg + TRIBE (segment only)
[Browser polls GET /v2/jobs/{id}]                       │  POST /v2/internal/hf-callback
                                                        ▼      (header X-NM-Token: <secret>)
                                                  [Lambda hf_callback]
                                                        ├─► [S3 results/{id}.json.gz]
                                                        └─► [DynamoDB] status=done
```

> Want the $0, no-account local loop instead? See
> [`infra/aws/sam-local.md`](../infra/aws/sam-local.md).

---

## Prerequisites

| Tool | Install (macOS) | Why |
|------|-----------------|-----|
| AWS CLI v2 | `brew install awscli` | Provisions + queries every AWS resource |
| SAM CLI | `brew install aws-sam-cli` | Wraps CloudFormation for the Lambda/API-Gateway stack |
| HuggingFace CLI | `pipx install "huggingface_hub[cli]"` | Pushes the Space repo |
| Node 20+ & pnpm | `brew install node pnpm` | Builds the web app |
| `git`, `rsync`, `curl`, `jq` | (preinstalled / `brew install jq`) | Space staging + the smoke test |

Docker is **not** required — `sam build` packages these pure-Python Lambdas by
copying source (there's no `requirements.txt`). You only need Docker for
`sam local`.

Verify your CLIs are wired up:

```bash
aws sts get-caller-identity      # prints your AWS account id
sam --version                    # 1.115+
hf whoami                        # prints your HF username
node --version                   # v20+
```

Pick a region and **stick with it** for the whole runbook:

```bash
export AWS_REGION=us-east-1       # cheapest; the $5 billing alarm only emits here
export STAGE=dev
export STACK=neural-media-aws     # matches infra/aws/samconfig.toml
```

---

## Step 1 — Deploy the HuggingFace Space (the GPU box)

The Space must exist first, because its URL is a deploy parameter for AWS in
Step 3.

### 1a. Create the Space

<https://huggingface.co/new-space>:

- **Owner**: your username
- **Space name**: `neural-media-tribe` (note what you pick)
- **SDK**: **Docker**
- **Hardware**: CPU basic to start (free); upgrade to A10G for real-time TRIBE
- **License**: `cc-by-nc-4.0`
- **Visibility**: Public is fine — the callback secret (Step 1b) is the auth layer.

### 1b. Generate the shared callback secret

This one value is shared by **two** parties: the AWS side stores it in SSM
(Step 2), the Space stores it as a secret. Generate it once now:

```bash
export NM_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "callback secret = $NM_SECRET"   # save it; you paste it twice
```

In the Space's **Settings → Variables and secrets**, add a **secret**:

- Name: `CALLBACK_SHARED_SECRET`
- Value: the `$NM_SECRET` value above

(The Space refuses every `/predict` request until this is set.)

### 1c. Push the code

The Space image needs three monorepo trees: `services/hf-space/` (app),
`services/inference/` (the TRIBE wrapper + region masks), and `shared/` (the
contracts package). Stage them together and push. From the monorepo root:

```bash
export HF_USER="$(hf whoami | head -1)"
export HF_SPACE="neural-media-tribe"

STAGE_DIR="$(mktemp -d)"
rsync -a --delete services/hf-space/  "$STAGE_DIR/"
mkdir -p "$STAGE_DIR/services"
rsync -a --delete services/inference/ "$STAGE_DIR/services/inference/"
rsync -a --delete shared/             "$STAGE_DIR/shared/"

cd "$STAGE_DIR"
git init -q
hf auth login                                  # opens a browser, one-time
git remote add space "https://huggingface.co/spaces/${HF_USER}/${HF_SPACE}"
git add -A
git -c user.email=deploy@neural-media -c user.name=deploy \
    commit -q -m "deploy $(date -u +%Y%m%dT%H%M%SZ)"
git push -f space HEAD:main
cd -
```

HF rebuilds automatically (first build ~10 min — it pulls TRIBE weights). Watch
the **Build logs** tab. When it ends with "Application startup complete":

```bash
export HF_SPACE_URL="https://${HF_USER}-${HF_SPACE}.hf.space"
curl -sf "${HF_SPACE_URL}/healthz" && echo " ✓ Space is up"
```

The URL shape is `https://<user>-<space>.hf.space` (the `/` between user and
space becomes `-`, everything lowercased). **Keep `$HF_SPACE_URL`** — Step 3
needs it.

> Build fails? Almost always a missing dep in `services/hf-space/requirements.txt`
> (owned by the HF-Space worker). Attach the build log when you report it.

---

## Step 2 — Store the callback secret in AWS SSM Parameter Store

The `jobs_worker` and `hf_callback` Lambdas fetch this secret **at cold start**
via `ssm:GetParameter` (it is *not* baked into the template — see
`infra/aws/lambdas/shared/__init__.py:get_callback_secret`). Write the same
value you gave the Space:

```bash
aws ssm put-parameter \
  --region "$AWS_REGION" \
  --name /neural-media/hf-callback-secret \
  --type SecureString \
  --value "$NM_SECRET"
```

`SecureString` encrypts at rest with the free default `aws/ssm` KMS key. The
**first write creates version 1** — that's the `HFCallbackSecretSsmVersion`
you pass in Step 3. Verify:

```bash
aws ssm get-parameter --region "$AWS_REGION" \
  --name /neural-media/hf-callback-secret --with-decryption \
  --query 'Parameter.Value' --output text     # should echo $NM_SECRET
```

If this errors, your IAM principal is missing `ssm:GetParameter` + `kms:Decrypt`.

---

## Step 3 — Deploy the AWS stack

```bash
cd infra/aws
sam build
sam deploy --guided        # first time only; saves answers to samconfig.toml
```

Answers for the `--guided` prompts:

| Prompt | Answer |
|--------|--------|
| Stack Name | `neural-media-aws` |
| AWS Region | `us-east-1` |
| `Stage` | `dev` |
| `HFSpaceUrl` | your `$HF_SPACE_URL` from Step 1 — base only, **no** `/predict` (the worker appends it) |
| `FrontendOrigin` | the deployed web origin (Step 5). Unknown on first deploy → use `http://localhost:3000` now and re-deploy after Step 5 |
| `HFCallbackSecretSsmName` | `/neural-media/hf-callback-secret` |
| `HFCallbackSecretSsmVersion` | `1` (the version from Step 2; bump on rotation) |
| `BillingAlarmEmail` | your email (or blank to subscribe later in Step 4) |
| Confirm changes before deploy | `y` |
| Allow SAM CLI IAM role creation | `y` |
| `JobsCreate*` may not have authorization defined | `y` (intentional — public POST) |
| Save arguments to configuration file | `y` |

Deploy takes ~3–5 min. Capture the **API base URL** from the outputs:

```bash
export API_BASE="$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)"
echo "API_BASE = $API_BASE"     # e.g. https://abc123.execute-api.us-east-1.amazonaws.com/dev
```

> **Re-deploys** after the first: `sam build && sam deploy` (no `--guided`).

This single stack creates **5 Lambdas** (`neural-media-${STAGE}-…`):

| Function | Route | Role |
|----------|-------|------|
| `jobs-create` | `POST /v2/jobs` | Validate `{url,startSec,endSec}`, write the job, async-invoke the worker |
| `jobs-upload` | `POST /v2/jobs/upload` + `…/{id}/confirm` | Presigned MP4 upload path (whole-file, no segment) |
| `jobs-worker` | *(async-invoked)* | POST `${HF_SPACE_URL}/predict` with the segment + callback token |
| `jobs-status` | `GET /v2/jobs/{id}` | Read DynamoDB; presign the `results/` object on `done` |
| `hf-callback` | `POST /v2/internal/hf-callback` | Verify `X-NM-Token`, write `results/`, mark `done` |

plus `JobsTable` (DynamoDB), `ResultsBucket` (S3), the HTTP API, and the billing
alarm. `jobs_worker` is pointed at the Space purely by the `HFSpaceUrl`
parameter (→ `HF_SPACE_URL` env var).

**If the deploy fails:** find the *first* `FAILED` event (not the last) with
`aws cloudformation describe-stack-events --region "$AWS_REGION" --stack-name "$STACK"`.

---

## Step 4 — Verify the $5 billing alarm (do not skip)

The template already provisions a CloudWatch alarm + SNS topic. One account-wide
toggle is required for the metric to flow: **AWS Console → Billing → Billing
preferences → enable "Receive Billing Alerts"** (no CLI for this). Then:

```bash
aws cloudwatch describe-alarms --region us-east-1 \
  --alarm-names "neural-media-${STAGE}-billing-over-5-usd" \
  --query 'MetricAlarms[0].StateValue' --output text     # OK or INSUFFICIENT_DATA
```

If you left `BillingAlarmEmail` blank, subscribe now:

```bash
TOPIC_ARN="$(aws cloudformation describe-stacks --region us-east-1 \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BillingAlarmTopicArn'].OutputValue" --output text)"
aws sns subscribe --region us-east-1 --topic-arn "$TOPIC_ARN" \
  --protocol email --notification-endpoint you@example.com
# …then click the "Confirm subscription" link in your inbox.
```

---

## Step 5 — Host the web app on S3 + CloudFront

The web app is a static client bundle that talks **only** to `$API_BASE` (no
Next.js server routes on the hot path), so it hosts cleanly on S3 + CloudFront.

> **Prerequisite (frontend-owned):** static export requires
> `output: 'export'` in `apps/web/next.config.ts` (and moving the security
> headers to a CloudFront response-headers policy, since `headers()` is a
> server feature). As of this writing `next.config.ts` does **not** set it —
> coordinate with the frontend worker before this step. With `output: 'export'`,
> `next build` writes the static site to `apps/web/out/`.

### 5a. Build with the API base baked in

`NEXT_PUBLIC_*` vars are inlined at **build** time. The frontend reads
**`NEXT_PUBLIC_API_BASE_V2`** (`apps/web/lib/api-v2.ts`):

```bash
cd apps/web
pnpm install
NEXT_PUBLIC_API_BASE_V2="$API_BASE" pnpm build    # writes ./out/
cd -
```

### 5b. Create the bucket + upload

```bash
export WEB_BUCKET="neural-media-${STAGE}-web-$(aws sts get-caller-identity --query Account --output text)"
aws s3 mb "s3://${WEB_BUCKET}" --region "$AWS_REGION"
aws s3 sync apps/web/out/ "s3://${WEB_BUCKET}/" --delete
```

### 5c. Put CloudFront in front (HTTPS + private bucket)

Create a CloudFront distribution with the S3 bucket as origin, locked down with
**Origin Access Control** (keep the bucket private), default root object
`index.html`, and a custom-error response mapping 403/404 → `/index.html` (so
client routes resolve). There's no one-liner — follow
<https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/getting-started-secure-static-website-cloudfront.html>.
Budget ~30 min the first time. Note the distribution domain, e.g.
`https://d1234abcd.cloudfront.net`.

### 5d. Close the CORS loop

Re-deploy the API so its CORS allow-list includes the real frontend origin:

```bash
cd infra/aws
sam deploy --parameter-overrides \
  "Stage=${STAGE} HFSpaceUrl=${HF_SPACE_URL} FrontendOrigin=https://d1234abcd.cloudfront.net \
   HFCallbackSecretSsmName=/neural-media/hf-callback-secret HFCallbackSecretSsmVersion=1"
cd -
```

(`http://localhost:3000` stays allowed in addition, for local dev.)

---

## Step 6 — Smoke test the deployment

One command runs the whole chain (create → poll → fetch → assert) against the
deployed API and prints a clear ✓/✗ per check:

```bash
scripts/smoke-test-single.sh "$API_BASE"
```

Expected tail:

```
  ✓ created job 9f0c…
    status → pending  (elapsedSec=0)
    status → downloading  (elapsedSec=4)
    status → inferring  (elapsedSec=11)
    status → done  (elapsedSec=46)
  ✓ job reached status=done
  ✓ fetched ActivationPayload
  ✓ videoDurationSec is a number
  ✓ modelVersion is a string
  ✓ timestamps is a non-empty number[] (n=120)
  ✓ byRegion has all 8 regions
  ✓ every region series is a number[] of length 120
PASS — single-video pipeline healthy (job 9f0c…)
```

It exits non-zero on any failure. Tunables (env): `SAMPLE_URL`, `START_SEC`,
`END_SEC`, `POLL_INTERVAL_SEC`, `TIMEOUT_SEC`. A real run is 60–120 s on a
free-tier Space.

| Symptom | Likely cause |
|---------|--------------|
| `status=failed_download error=download_blocked` | YouTube rate-limited the Space's IP. Re-run; if persistent, set a proxy in the Space. |
| `status=rejected_duration error=segment_out_of_bounds` | `endSec` exceeds the real video length. Pick a smaller window. |
| Polls return 5xx | Check `jobs_create` / `jobs_status` CloudWatch logs — usually a wrong `HFSpaceUrl` or a missing SSM secret. |
| `status=done` but no `resultUrl` | `hf_callback` failed to write S3 — check its logs + IAM. |
| Times out | Free-tier Spaces hibernate; `curl ${HF_SPACE_URL}/healthz` to warm it, then re-run. |

Finally, open the CloudFront URL and run the same flow from the UI.

---

## Secrets & parameters you must set (checklist)

| Where | Name | Value |
|-------|------|-------|
| HF Space secret | `CALLBACK_SHARED_SECRET` | the generated `$NM_SECRET` |
| AWS SSM (SecureString) | `/neural-media/hf-callback-secret` | the **same** `$NM_SECRET` |
| `sam deploy` param | `HFSpaceUrl` | `https://<user>-<space>.hf.space` |
| `sam deploy` param | `FrontendOrigin` | the CloudFront URL (Step 5d) |
| `sam deploy` param | `HFCallbackSecretSsmVersion` | `1` (bump on rotation) |
| `sam deploy` param | `BillingAlarmEmail` | optional |
| Frontend build env | `NEXT_PUBLIC_API_BASE_V2` | the `ApiEndpoint` (`$API_BASE`) |

**Rotating the secret:** `aws ssm put-parameter … --overwrite` (note the new
version), update the Space's `CALLBACK_SHARED_SECRET` to match, then
`sam deploy --parameter-overrides HFCallbackSecretSsmVersion=<new>`. There is no
overlap window — do both sides together.

---

## Rollback / teardown

```bash
# 1. Empty the results bucket (sam delete refuses a non-empty bucket):
aws s3 rm "s3://$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ResultsBucketName'].OutputValue" --output text)" --recursive

# 2. Delete the AWS stack (API GW, Lambdas, DynamoDB, S3, alarm, SNS):
sam delete --region "$AWS_REGION" --stack-name "$STACK"

# 3. Empty + delete the web bucket and its CloudFront distribution:
aws s3 rm "s3://${WEB_BUCKET}" --recursive && aws s3 rb "s3://${WEB_BUCKET}"
#    (disable, then delete the CloudFront distribution in the console)

# 4. Delete the SSM secret (not part of the stack):
aws ssm delete-parameter --region "$AWS_REGION" --name /neural-media/hf-callback-secret
```

Hand-clean what has no `sam`/CLI hook:

- **The HF Space** — Space → Settings → "Delete this Space".
- **CloudWatch log groups** — `aws logs delete-log-group --log-group-name /aws/lambda/neural-media-${STAGE}-jobs-create` (repeat per function); free under 5 GB.

After all of the above your monthly bill returns to **$0.00**.
