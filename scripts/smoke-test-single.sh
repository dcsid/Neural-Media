#!/usr/bin/env bash
# Single-video pipeline end-to-end smoke test.
#
# Usage:
#     API_BASE=https://abc123.execute-api.us-east-1.amazonaws.com \
#       scripts/smoke-test-single.sh
#
# Or against `sam local`:
#     API_BASE=http://127.0.0.1:3001 scripts/smoke-test-single.sh
#
# Exercises:
#     POST /v2/jobs               -> { jobId }
#     GET  /v2/jobs/{jobId}       -> poll until terminal
#     GET  resultUrl              -> activation JSON (gzip'd at rest, but
#                                    presigned S3 URL serves it transparent)
#
# Deps: curl + jq. Nothing else. No AWS credentials needed — API_BASE is
# unauthenticated for the demo (rate-limited at the gateway).
#
# Exit codes:
#     0   status=done, byRegion has all eight regions, exit clean
#     1   pipeline ran but terminated in a failed_* state
#     2   timed out before reaching a terminal status
#     3   pre-flight problem (API_BASE missing, jq missing, etc.)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# A short, public TikTok video. If TikTok blocks the request the mock
# Space's 1/8 simulated failure path will trip eventually — re-run.
SAMPLE_URL="${SAMPLE_URL:-https://www.tiktok.com/@scout2015/video/6718335390845095173}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-3}"
TIMEOUT_SEC="${TIMEOUT_SEC:-180}"

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

if [[ -z "${API_BASE:-}" ]]; then
  echo "ERROR: API_BASE is required." >&2
  echo "  e.g. API_BASE=http://127.0.0.1:3001 $0" >&2
  exit 3
fi
# Strip any trailing slash so we never emit a URL with `//`.
API_BASE="${API_BASE%/}"

for bin in curl jq; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: \`$bin\` not on PATH." >&2
    exit 3
  fi
done

echo "==> API_BASE = $API_BASE"
echo "==> sample URL = $SAMPLE_URL"

# ---------------------------------------------------------------------------
# 1. Create the job
# ---------------------------------------------------------------------------

create_resp="$(curl --fail-with-body -sS -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"${SAMPLE_URL}\"}" \
  "${API_BASE}/v2/jobs")"

job_id="$(jq -r '.jobId // empty' <<<"$create_resp")"
if [[ -z "$job_id" ]]; then
  echo "ERROR: POST /v2/jobs did not return a jobId." >&2
  echo "  response: $create_resp" >&2
  exit 1
fi
echo "==> created jobId=$job_id"

# ---------------------------------------------------------------------------
# 2. Poll until terminal or timeout
# ---------------------------------------------------------------------------

deadline=$(( $(date +%s) + TIMEOUT_SEC ))
status="unknown"
last_status=""
result_url=""
error_msg=""

while :; do
  if (( $(date +%s) >= deadline )); then
    echo "ERROR: timed out after ${TIMEOUT_SEC}s in status=${status}" >&2
    exit 2
  fi

  poll="$(curl --fail-with-body -sS "${API_BASE}/v2/jobs/${job_id}")"
  status="$(jq -r '.status // "unknown"' <<<"$poll")"
  if [[ "$status" != "$last_status" ]]; then
    echo "    status -> $status  (elapsedSec=$(jq -r '.elapsedSec // 0' <<<"$poll"))"
    last_status="$status"
  fi

  case "$status" in
    done)
      result_url="$(jq -r '.resultUrl // empty' <<<"$poll")"
      break
      ;;
    failed_download|failed_inference|rejected_duration)
      error_msg="$(jq -r '.error // "(no error message)"' <<<"$poll")"
      echo "FAIL: terminal status=$status error=$error_msg" >&2
      exit 1
      ;;
    pending|downloading|inferring)
      sleep "$POLL_INTERVAL_SEC"
      ;;
    *)
      echo "WARN: unexpected status=$status — continuing to poll" >&2
      sleep "$POLL_INTERVAL_SEC"
      ;;
  esac
done

# ---------------------------------------------------------------------------
# 3. Fetch and validate the result
# ---------------------------------------------------------------------------

if [[ -z "$result_url" ]]; then
  echo "ERROR: status=done but no resultUrl in response" >&2
  exit 1
fi
echo "==> fetching resultUrl"
# `--compressed` lets curl request gzip transfer-encoding so the S3 object
# (stored as application/gzip) is decoded on the fly. If the bucket
# returns it as octet-stream we'll need to gunzip manually — try both.
result_json="$(curl --fail-with-body --compressed -sS "$result_url" || true)"
if ! jq -e . <<<"$result_json" >/dev/null 2>&1; then
  # Probably raw gzip. Re-fetch as binary and gunzip.
  tmp="$(mktemp)"
  curl --fail-with-body -sS "$result_url" -o "$tmp"
  result_json="$(gunzip -c "$tmp")"
  rm -f "$tmp"
fi

echo "==> result summary"
jq -r '
  "  videoDurationSec: \(.videoDurationSec)",
  "  modelVersion:     \(.modelVersion)",
  "  byRegion keys:    \(.byRegion | keys | join(", "))",
  "  timestamps[0..2]: \(.timestamps[0:3])"
' <<<"$result_json"

# Contract assertion: every region we ship in the frontend must be present.
expected_regions='["v1","v2","v3","v4","auditory","language","ffa","vwfa"]'
missing="$(jq -r --argjson expected "$expected_regions" '
  $expected - (.byRegion | keys) | join(", ")
' <<<"$result_json")"
if [[ -n "$missing" ]]; then
  echo "FAIL: byRegion is missing: $missing" >&2
  exit 1
fi

echo "OK"
