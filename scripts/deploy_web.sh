#!/usr/bin/env bash
# Build + deploy the web app to S3/CloudFront with correct cache headers.
#
# The reason this script exists: a plain `aws s3 sync` uploads with NO
# Cache-Control header, so browsers heuristically cache the HTML and a
# returning visitor sees a stale page after a deploy until they hard-reload
# (CloudFront invalidation clears the CDN, not anyone's browser). Strategy:
#
#   *.html / *.txt     public, max-age=0, must-revalidate   (always fresh)
#   _next/static/**    public, max-age=31536000, immutable  (content-hashed)
#   everything else    public, max-age=86400                (media/data)
#
# NB: never "fix" headers with an in-place `aws s3 cp --metadata-directive
# REPLACE` — on S3→S3 copies the CLI does not re-derive Content-Type, so every
# object degrades to binary/octet-stream and browsers DOWNLOAD pages instead of
# rendering them (bit us 2026-07-05). Always re-upload from local out/ instead;
# local uploads re-guess the MIME type from each file's extension.
#
# Usage:
#   NEXT_PUBLIC_API_BASE_V2=https://<api-id>.execute-api.us-east-1.amazonaws.com/dev \
#     scripts/deploy_web.sh
#
# Optional env: WEB_BUCKET, CF_DIST_ID, SKIP_BUILD=1 (reuse apps/web/out).
set -euo pipefail

cd "$(dirname "$0")/.."
BUCKET="${WEB_BUCKET:-neural-media-demo-817866065510}"
DIST="${CF_DIST_ID:-E3MT7NNX14Y7KO}"
: "${NEXT_PUBLIC_API_BASE_V2:?set NEXT_PUBLIC_API_BASE_V2 to the API Gateway base URL}"

if [[ "${SKIP_BUILD:-}" != "1" ]]; then
  echo "→ building static export (API base: ${NEXT_PUBLIC_API_BASE_V2})"
  STATIC_EXPORT=1 pnpm --filter @neural-media/web build
fi

OUT=apps/web/out
[[ -d "$OUT" ]] || { echo "✗ $OUT missing — did the build fail?" >&2; exit 1; }

echo "→ media + data (1-day cache)"
aws s3 sync "$OUT" "s3://$BUCKET" --delete \
  --exclude "*.html" --exclude "*.txt" --exclude "_next/static/*" \
  --cache-control "public, max-age=86400"

echo "→ hashed assets (immutable)"
aws s3 sync "$OUT/_next/static" "s3://$BUCKET/_next/static" --delete \
  --cache-control "public, max-age=31536000, immutable"

# Documents go LAST so a fresh page is never live before the assets it references.
echo "→ documents (always revalidate)"
aws s3 sync "$OUT" "s3://$BUCKET" --delete \
  --exclude "*" --include "*.html" --include "*.txt" \
  --cache-control "public, max-age=0, must-revalidate"

echo "→ invalidating CloudFront"
ID=$(aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/*' \
  --query 'Invalidation.Id' --output text)
echo "✓ deployed — invalidation $ID propagating (~1-2 min). To block on it:"
echo "  aws cloudfront wait invalidation-completed --distribution-id $DIST --id $ID"
