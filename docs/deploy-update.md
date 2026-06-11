# Shipping an update to the live deploy

This is the **update** runbook — pushing a code change to the already-live demo
(Space + Lambdas + frontend). For from-scratch setup (creating the Space, the
SSM secret, the stack), see [`single-video-deploy.md`](single-video-deploy.md).

This change touches all three surfaces, so all three deploy. They're
independent, but a good order is **Lambdas → Space → Frontend** (the Lambdas
must be ready to accept the Space's new progress callbacks before the Space
starts sending them; the frontend can go any time).

Everything below **discovers your real values from the live AWS stack** rather
than hardcoding them — the committed `samconfig.toml` only has placeholders.

```bash
export AWS_REGION=us-east-1
export STACK=neural-media-aws
```

---

## Step 0 — Pull your live values (don't skip)

```bash
# The deployed API base URL the frontend talks to:
export API_BASE="$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue" --output text)"

# The parameters the stack is CURRENTLY deployed with (so a redeploy doesn't
# silently change the wiring — the committed samconfig.toml has REPLACE-ME):
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK" \
  --query "Stacks[0].Parameters" --output table
```

From that Parameters table, capture the live values into env vars (copy the
real ones you see):

```bash
export HF_SPACE_URL="https://<you>-neural-media-tribe.hf.space"   # = the live HFSpaceUrl param
export FRONTEND_ORIGIN="https://ddbk4djj9nrdg.cloudfront.net"     # = the live FrontendOrigin param
echo "API_BASE=$API_BASE"
```

---

## Step 1 — Lambdas (SAM)

Ships the `hf_callback` (stores stage/progress), `jobs_status` (returns them),
and the shared `JOB_STAGES`/`clamp_progress` changes.

```bash
cd infra/aws
sam build
# Pass the live values explicitly so the redeploy keeps the wiring intact
# (guards against the placeholder samconfig). Keeps the secret params as-is.
sam deploy --parameter-overrides \
  "Stage=dev \
   HFSpaceUrl=$HF_SPACE_URL \
   FrontendOrigin=$FRONTEND_ORIGIN \
   HFCallbackSecretSsmName=/neural-media/hf-callback-secret \
   HFCallbackSecretSsmVersion=1"
cd -
```

SAM shows a changeset (it'll list the two function updates) — confirm `y`. ~2–3
min. Pure-Python Lambdas; no Docker needed.

---

## Step 2 — HuggingFace Space

Ships the Dockerfile baking (spaCy + persistent uv cache), the progress pings,
and the device-check log. **This is the only way the Space gets the new code** —
sleeping/waking reuses the old image; it does not rebuild.

```bash
# from repo root; <you> = your `hf whoami`
hf auth login                              # one-time, if not already
scripts/deploy_hf_space.sh <you>           # assembles the 3-tree snapshot + force-pushes
```

Then, as the script prints:

- Watch the **Building** tab on the Space (first build after the Dockerfile
  change is ~10 min — it bakes the deps).
- When it reports "Application startup complete":
  ```bash
  curl -sf "$HF_SPACE_URL/healthz" && echo ' ✓ up'
  ```
- **Warm it once**: run a throwaway upload through the site and let it finish, so
  the first real visitor doesn't pay the one-time weight download.

> If you ever change the Space's `HFSpaceUrl`, re-run Step 1 with the new value.
> Optional: in the Space **Settings → Variables**, set `HF_MAX_DURATION_SEC=45`
> to bound worst-case job time until a GPU upgrade.

---

## Step 3 — Frontend (S3 + CloudFront)

Ships the progress bar, the live sub-stage labels, and the 20-minute timeout.

```bash
# Build the static export with the live API base inlined (NEXT_PUBLIC_* is
# baked at build time):
NEXT_PUBLIC_API_BASE_V2="$API_BASE" STATIC_EXPORT=1 \
  pnpm --filter @neural-media/web build      # writes apps/web/out/

# Find the live web bucket + CloudFront distribution behind the demo domain:
aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(DomainName,'ddbk4djj9nrdg')].[Id,Origins.Items[0].DomainName]" \
  --output table
# Expected (per the last gallery deploy): dist E3MT7NNX14Y7KO,
# bucket neural-media-demo-817866065510. Use what the command prints.

export WEB_BUCKET=neural-media-demo-817866065510
export WEB_DIST=E3MT7NNX14Y7KO

aws s3 sync apps/web/out "s3://$WEB_BUCKET" --delete
aws cloudfront create-invalidation --distribution-id "$WEB_DIST" --paths '/*'
```

---

## Step 4 — Verify

```bash
# Backend chain end-to-end:
scripts/smoke-test-single.sh "$API_BASE"
```

Then open <https://ddbk4djj9nrdg.cloudfront.net/>, upload a short clip, and watch
the tracking panel — you should now see the sub-stage label advance
("Transcribing the audio" → "Encoding the video" → …) with a determinate bar,
and the job should no longer fail at 10 minutes.

After the next run, check the Space logs for the one-shot
`event=device-check … model_param_device=…` line — confirm it's `cuda`, not
`cpu`, before considering a GPU upgrade.
