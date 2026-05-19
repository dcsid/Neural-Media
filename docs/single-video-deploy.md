# Single-Video Pipeline — Deployment Runbook

This is the from-scratch deploy of the **v2 single-video architecture**:
a user pastes a TikTok URL into the web app, an AWS API Gateway routes
the request through a Lambda chain to a HuggingFace Space that runs the
real TRIBE model, and the results land in S3 + DynamoDB and surface back
in the browser.

**You do not need to know AWS to follow this.** Every step lists the
exact commands or the exact console clicks. If you get stuck at a step,
the troubleshooting block underneath usually has the answer.

```
[Browser /single page]
     │
     │ POST /v2/jobs { url }
     ▼
[API Gateway] ──► [Lambda jobs_create] ──► [DynamoDB jobs table]
                            │
                            │ async invoke
                            ▼
                    [Lambda jobs_worker] ──► [HF Space /predict]
                                                    │ (yt-dlp, ffmpeg, TRIBE)
                                                    │
[Browser polls GET /v2/jobs/{id}]                   │ POST /v2/internal/hf-callback
                                                    ▼
                                              [Lambda hf_callback]
                                                    │
                                                    ├──► [S3 results/{id}.json.gz]
                                                    └──► [DynamoDB] mark done
```

If you only want to run this locally (no AWS account, no HuggingFace
account) read `infra/aws/sam-local.md` instead — that path costs $0 and
takes about 10 minutes.

---

## Prerequisites

Install once. Each link is the upstream install doc; the bracketed
command is the macOS happy path.

| Tool                  | Why                                                              |
|-----------------------|------------------------------------------------------------------|
| [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) `[brew install awscli]`           | Provisions every AWS resource below |
| [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) `[brew install aws/tap/aws-sam-cli]` | Wraps CloudFormation for the Lambda + API Gateway template |
| [HuggingFace CLI](https://huggingface.co/docs/huggingface_hub/installation) `[pipx install huggingface_hub[cli]]` | Pushes the Space repo                |
| [pnpm](https://pnpm.io/installation) `[brew install pnpm]`                       | Builds the Next.js web app           |
| [Vercel CLI](https://vercel.com/docs/cli) (optional) `[pnpm i -g vercel]`        | Deploys the web app                  |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) `[brew install --cask docker]` | Required by SAM for container builds |

Accounts you need before starting:

- **AWS** account with billing enabled. The whole demo fits in the
  free tier; the $5 alarm in step 4 is your insurance.
- **HuggingFace** account with at least one Space created. Free CPU tier
  works for the mock-resolution demo; A10G or A100 is needed for full TRIBE.
- **Vercel** account if you go with Vercel for the frontend. Skip if
  you'd rather host on S3 + CloudFront (covered in step 5).

Verify the CLI tools are wired:

```bash
aws sts get-caller-identity      # should print your account id
sam --version                    # 1.115+ recommended
hf whoami                        # prints your HF username
node --version                   # 20.x
pnpm --version                   # 9.x
```

---

## Step 1 — Create and deploy the HuggingFace Space

This is the box that actually runs TRIBE. Pushing it before AWS makes
the AWS step simpler (the Space URL exists when you populate SSM in
step 2).

1. **Create the Space.** Go to <https://huggingface.co/new-space>:
   - Owner: your username
   - Space name: `neural-media-tribe` (or similar — note what you pick)
   - License: cc-by-nc-4.0
   - SDK: **Docker**
   - Hardware: **CPU basic** to get going (free); upgrade to A10G later
     if you want real inference speed
   - Visibility: Public is fine for the demo. The callback secret in
     step 2 is the auth layer.

2. **Set the callback secret env var.** In the Space's *Settings →
   Variables and secrets*:
   - New secret, name `CALLBACK_SHARED_SECRET`, value: any 32-char
     random string. Generate with `openssl rand -base64 24`. **Save
     this value** — it goes into SSM in step 2.

3. **Push the code.** From the repo root:

   ```bash
   cd services/hf-space
   git init -b main                           # if not already
   hf auth login                              # opens a browser
   git remote add hf https://huggingface.co/spaces/<your-user>/neural-media-tribe
   git add -A
   git commit -m "Deploy single-video Space"
   git push hf main
   ```

   The Space rebuilds automatically (the first build takes ~10 min
   because it pulls TRIBE weights). Watch the Build logs tab.

4. **Confirm it's up.** When the Build logs end with "Application
   startup complete", visit:

   ```
   https://<your-user>-neural-media-tribe.hf.space/health
   ```

   You should see `{"status":"ok"}`. **Write down that URL** — it goes
   into SSM in step 2 too. The shape is `https://<user>-<space>.hf.space`
   with the `/` between user and space replaced by `-` and any uppercase
   lowercased. Spaces will redirect from the canonical URL if you get
   it slightly wrong.

If the build fails, the most common cause is a missing dependency in
`services/hf-space/requirements.txt`. T2 owns that file — open an issue
with the Space's build log attached.

> **Sanity tip:** `make deploy-hf-space` prints these same commands so
> you don't have to come back here when re-pushing.

---

## Step 2 — Store the callback secret in AWS Systems Manager Parameter Store

The SAM template resolves the callback secret from SSM at deploy time
(via `{{resolve:ssm-secure:…}}`) so it never appears in the
CloudFormation template body. The HF Space URL is different — it goes
straight into `sam deploy` as a plain CloudFormation parameter in
Step 3, no SSM round-trip — so this step only writes the secret.

> The `Stage` parameter is a separate axis from the SSM path, so the
> default SSM name has no `/dev/` segment. Keep one secret entry per
> rotation generation, not per stage.

```bash
# Pick a region and stick with it for the whole runbook. us-east-1 is
# cheapest and has every service we use.
export AWS_REGION=us-east-1

aws ssm put-parameter \
  --region "$AWS_REGION" \
  --name /neural-media/hf-callback-secret \
  --type SecureString \
  --value "<the same value you set as CALLBACK_SHARED_SECRET in step 1>" \
  --overwrite
```

`SecureString` encrypts at rest with the default `aws/ssm` KMS key — free.
Verify:

```bash
aws ssm get-parameter --name /neural-media/hf-callback-secret --with-decryption
```

The call should return the secret in plaintext. If it does not, your IAM
user is missing `ssm:GetParameter` + `kms:Decrypt` — add them.

Note the HF Space URL from Step 1 — you'll paste it into
`sam deploy --guided` as the `HFSpaceUrl` parameter in the next step.

---

## Step 3 — Deploy the AWS stack

```bash
cd infra/aws
sam build
sam deploy --guided
```

The `--guided` flow asks a sequence of questions. Sensible answers:

| Prompt                                          | Answer                                  |
|-------------------------------------------------|-----------------------------------------|
| Stack Name                                      | `neural-media-dev`                      |
| AWS Region                                      | `us-east-1`                             |
| Confirm changes before deploy                   | `y` (review each time)                  |
| Allow SAM CLI IAM role creation                 | `y` (creates the Lambda execution role) |
| Disable rollback                                | `n`                                     |
| `Stage` parameter                               | `dev` (slug used in resource names)     |
| `HFSpaceUrl` parameter                          | the `https://<user>-<space>.hf.space` URL from Step 1, no trailing path — `jobs_worker` appends `/predict` itself |
| `FrontendOrigin` parameter                      | the deployed web app origin (e.g. `https://neural-media.vercel.app`); `http://localhost:3000` is always allowed in addition |
| `HFCallbackSecretSsmName` parameter             | `/neural-media/hf-callback-secret` (the name you used in Step 2) |
| `HFCallbackSecretSsmVersion` parameter          | `1` (bump on rotation; CFN requires an explicit version for `ssm-secure` refs) |
| `BillingAlarmEmail` parameter                   | the email to subscribe to the $5 alarm — leave blank to skip, see Step 4 |
| `JobsCreate` may not have authorization defined | `y` (intentional — public POST)         |
| Save arguments to configuration file            | `y` → `samconfig.toml`                  |

Wait ~3-5 minutes. The final output lists the resources; the bit you
need is **Outputs.ApiEndpoint** — something like

```
https://abc123xyz.execute-api.us-east-1.amazonaws.com/dev
```

That is `$API_BASE` for the rest of the runbook.

> **Re-deploys** after this point: `make deploy-aws` (no `--guided`).
> The `samconfig.toml` file remembers your answers.

If the deploy fails:

- *"Resource creation cancelled"* → CloudFormation rolled back. Run
  `aws cloudformation describe-stack-events --stack-name neural-media-dev`
  to find the actual error (it's the *first* FAILED event, not the
  last one).
- *"S3 bucket does not exist"* — SAM stages artifacts in an S3 bucket.
  Run `sam deploy --guided` again and answer `y` to "Create managed
  artifact bucket".
- *"Cannot find module ..."* in a Lambda — T3's `requirements.txt`
  is missing the dep. File a bug with the CloudWatch error.

---

## Step 4 — ⚠️ Verify the $5 billing alarm (do not skip)

A misconfigured Lambda or HF Space callback loop can rack up real money
in real hours. The SAM template you deployed in Step 3 **already
provisions** the alarm, an SNS topic, and (if you supplied
`BillingAlarmEmail`) an email subscription:

- `BillingAlarm` (CloudWatch) — fires when `AWS/Billing > EstimatedCharges` exceeds `$5` for one 6-hour period
- `BillingAlarmTopic` (SNS) — the notification fan-out
- `BillingAlarmEmailSubscription` (SNS) — only created when you set the `BillingAlarmEmail` parameter; otherwise subscribe by hand below

> The alarm metric only emits in `us-east-1` regardless of where your
> resources actually live. If you deployed the stack to another region,
> the template won't have an alarm — re-run `sam deploy` with
> `--region us-east-1` (or live with manually creating the alarm there
> using the recipe in this doc's git history).

### 4a — One-time: enable billing alerts on the account

If you have never received an AWS billing alert before, flip the
account-wide switch first: AWS Console → Billing → Billing preferences
→ check **"Receive Billing Alerts"** → Save preferences. There is no
CLI for this; it's an Org Billing toggle. Without it the alarm fires
but the metric stays at `INSUFFICIENT_DATA` forever.

### 4b — Verify the alarm exists

```bash
aws cloudwatch describe-alarms \
  --region us-east-1 \
  --alarm-names "neural-media-${STAGE:-dev}-billing-over-5-usd" \
  --query 'MetricAlarms[0].StateValue'
```

Expect `"INSUFFICIENT_DATA"` (no spend yet) or `"OK"`. If the call
returns an empty result, the deploy didn't land the alarm — re-check
the region and the stack output.

### 4c — Subscribe an email if you skipped `BillingAlarmEmail`

If you left the `BillingAlarmEmail` parameter blank in Step 3, the
SNS topic exists but has no subscribers. Add one:

```bash
TOPIC_ARN=$(aws cloudformation describe-stacks \
  --region us-east-1 \
  --stack-name neural-media-dev \
  --query "Stacks[0].Outputs[?OutputKey=='BillingAlarmTopicArn'].OutputValue" \
  --output text)

aws sns subscribe \
  --region us-east-1 \
  --topic-arn "$TOPIC_ARN" \
  --protocol email \
  --notification-endpoint <your-email@example.com>
```

You'll get a confirmation email. **Click the "Confirm subscription"
link** — without it the alarm fires but no email goes out.

Alternatively, re-run `sam deploy --parameter-overrides BillingAlarmEmail=<your-email@example.com>` and CFN will create the subscription for you.

---

## Step 5 — Deploy the web app

Two paths. Pick by which infra you'd rather pay for.

### Option A — Vercel (recommended for the first deploy)

```bash
cd apps/web
cp .env.example .env.production
# Edit .env.production: NEXT_PUBLIC_API_BASE_V2=<the ApiEndpoint from step 3>
vercel link            # one-time, creates .vercel/project.json
vercel deploy --prod
```

Vercel reads `apps/web/.env.production` at build time and inlines
`NEXT_PUBLIC_*` vars into the client bundle. Output is something like
`https://neural-media-<hash>.vercel.app`. Open that and paste a TikTok
URL into the single-video page.

> Vercel's hobby tier is free up to 100 GB/mo bandwidth, well above the
> demo's needs.

### Option B — S3 + CloudFront (AWS-only)

Next.js 15 removed the standalone `next export` command. Static export
is now produced by `next build` when `output: 'export'` is set in
`next.config.ts`:

```ts
// apps/web/next.config.ts
const config: NextConfig = {
  output: 'export',
  // images.unoptimized = true if you use next/image, otherwise omit
};
```

Then:

```bash
cd apps/web
pnpm install
NEXT_PUBLIC_API_BASE_V2=https://abc123xyz.execute-api.us-east-1.amazonaws.com/dev \
  pnpm next build
# next build now writes the static site to apps/web/out/
aws s3 sync out/ s3://neural-media-web-<your-suffix>/ --delete
```

Then create a CloudFront distribution pointing at the bucket, default
root object `index.html`, with an Origin Access Identity. There is no
1-liner for this — follow
<https://docs.aws.amazon.com/AmazonS3/latest/userguide/website-hosting-cloudfront-walkthrough.html>
end to end. Budget 30 minutes the first time.

`output: 'export'` disables every server-side Next.js feature (route
handlers, server actions, ISR, on-demand revalidation). The
single-video flow itself works because every API call goes to
`NEXT_PUBLIC_API_BASE_V2` (the AWS HTTP API) and not to a Next
server route. If a future route adds server-only behaviour, switch
back to Option A or host on Lambda@Edge.

---

## Step 6 — Smoke test

From the repo root:

```bash
API_BASE=https://abc123xyz.execute-api.us-east-1.amazonaws.com/dev \
  make smoke-single
```

Expected output:

```
==> API_BASE = https://abc123xyz.execute-api.us-east-1.amazonaws.com/dev
==> sample URL = https://www.tiktok.com/@scout2015/video/6718335390845095173
==> created jobId=01H...
    status -> pending  (elapsedSec=0)
    status -> downloading  (elapsedSec=2)
    status -> inferring  (elapsedSec=12)
    status -> done  (elapsedSec=47)
==> fetching resultUrl
==> result summary
  videoDurationSec: 6.2
  modelVersion:     tribe-v2@2.1.0
  byRegion keys:    auditory, ffa, language, v1, v2, v3, v4, vwfa
  timestamps[0..2]: [0,0.25,0.5]
OK
```

A run typically takes 60-120s on a free-tier Space. If it errors out:

| Symptom                                       | Likely cause                                                                                   |
|-----------------------------------------------|------------------------------------------------------------------------------------------------|
| `status=failed_download` `error=tiktok_blocked` | TikTok rate-limited the Space's IP. Re-run; if persistent, configure a proxy in the Space.    |
| Polls hit `INSUFFICIENT_DATA` / 5xx           | Check CloudWatch Logs for `jobs_create` — likely the Space URL in SSM is wrong.                |
| `status=done` but no resultUrl                | `hf_callback` Lambda failed to write S3 — check its CloudWatch logs and IAM role permissions. |
| Smoke times out at 180s                       | Free-tier Spaces hibernate. Hit `/health` once to warm it, then re-run.                        |

The web app at the Vercel/CloudFront URL should now successfully run
the same flow when you paste a URL in.

---

## Rollback

If you need to tear it all down:

```bash
# 1. AWS stack
cd infra/aws
sam delete --stack-name neural-media-dev          # destroys API GW, Lambdas, DynamoDB, S3 (if empty)

# If S3 has objects, sam delete refuses. Empty it first:
aws s3 rm s3://<results-bucket-name> --recursive
sam delete --stack-name neural-media-dev

# 2. SSM parameter (only the secret — the Space URL was never written to SSM)
aws ssm delete-parameter --name /neural-media/hf-callback-secret

# 3. Billing alarm + SNS topic — already gone if `sam delete` succeeded
#    above (they're part of the stack). The block below is only for the
#    legacy case where you created the alarm by hand from an older
#    version of this runbook.
# aws cloudwatch delete-alarms --region us-east-1 \
#   --alarm-names neural-media-billing-over-5usd
# aws sns delete-topic --region us-east-1 --topic-arn "$TOPIC_ARN"
```

Hand-cleanup the following (no CLI for them):

- **The HF Space.** huggingface.co → your Space → Settings → "Delete
  this Space" at the bottom.
- **The Vercel project** (if you used Option A). vercel.com → project
  → Settings → "Delete Project".
- **CloudWatch Logs.** Each Lambda's log group lingers. They're free
  for the first 5 GB; delete via console or
  `aws logs delete-log-group --log-group-name /aws/lambda/<name>`.

After all of the above, your AWS bill for this project should go to
$0.00 next month.

---

## What's next

- The doc above provisions a **dev** environment (`/dev` stage,
  `neural-media-dev-...` resource names). A staging/prod split would
  duplicate everything under `/neural-media/prod/...` SSM keys and a
  `neural-media-prod` stack. Don't bother for the demo.
- The HF Space currently auto-hibernates on the free tier — first
  request after a quiet period takes ~30s longer. Upgrade to A10G if
  the latency bothers you (~$0.60/hr; pause when not demoing).
- The web app's per-request rate limit is API Gateway's default
  (10,000 r/s account-wide, 5,000 burst). For the demo this is
  effectively infinite. If you go public, add a usage plan with an
  API key.
- See `infra/aws/sam-local.md` for the iterate-faster local loop. Use
  it instead of `sam deploy` during development — the redeploy cycle
  on AWS is 60-90 seconds.
