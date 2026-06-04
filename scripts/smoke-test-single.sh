#!/usr/bin/env bash
# Post-deploy smoke test for the v2 single-video pipeline.
#
# Drives the full chain against a DEPLOYED (or mock) API base URL:
#     POST /v2/jobs { url, startSec, endSec }   -> 201 { jobId }
#     GET  /v2/jobs/{jobId}                       -> poll until terminal
#     GET  resultUrl                              -> ActivationPayload JSON
# then asserts the ActivationPayload shape (CONTRACTS.md §13.3, mirrored by
# apps/web/lib/api-v2.ts validateActivation):
#     - the 8 canonical regions are all present
#     - every region's series is a number[] the SAME length as `timestamps`
#
# Usage:
#     scripts/smoke-test-single.sh https://abc123.execute-api.us-east-1.amazonaws.com/dev
#     API_BASE=http://localhost:3001 scripts/smoke-test-single.sh   # e2e mock-server
#
# Tunables (env): SAMPLE_URL START_SEC END_SEC POLL_INTERVAL_SEC TIMEOUT_SEC
#
# Deps: curl + jq. No AWS credentials needed — the demo API is unauthenticated
# (rate-limited at the gateway).
#
# Exit codes:
#     0   status=done and the ActivationPayload passed every assertion
#     1   pipeline reached a failed_* status, or a shape assertion failed
#     2   timed out before reaching a terminal status
#     3   pre-flight problem (API base / curl / jq missing)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# API base may be arg 1 or $API_BASE. Trailing slash is stripped below.
API_BASE="${1:-${API_BASE:-}}"

# "Me at the zoo" — the first YouTube video, ~19s, reliably public. The
# [0s, 18s) window stays comfortably under the 90s segment cap.
SAMPLE_URL="${SAMPLE_URL:-https://www.youtube.com/watch?v=jNQXAC9IVRw}"
START_SEC="${START_SEC:-0}"
END_SEC="${END_SEC:-18}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-3}"
TIMEOUT_SEC="${TIMEOUT_SEC:-180}"

# The frontend ships these 8 regions; the result must carry all of them
# (must match shared REGION_IDS / api-v2.ts).
EXPECTED_REGIONS='["v1","v2","v3","v4","auditory","language","ffa","vwfa"]'

if [[ -t 1 ]]; then G=$'\033[32m'; R=$'\033[31m'; Z=$'\033[0m'; else G=""; R=""; Z=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$Z" "$1"; }
fail() { printf '  %s✗%s %s\n' "$R" "$Z" "$1" >&2; }

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

if [[ -z "$API_BASE" ]]; then
  fail "API base URL is required (pass as arg 1 or set \$API_BASE)."
  echo "    e.g. $0 https://abc123.execute-api.us-east-1.amazonaws.com/dev" >&2
  echo "    or   API_BASE=http://localhost:3001 $0" >&2
  exit 3
fi
API_BASE="${API_BASE%/}"

for bin in curl jq; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    fail "\`$bin\` not on PATH."
    exit 3
  fi
done

echo "==> API base : $API_BASE"
echo "==> sample   : $SAMPLE_URL  [${START_SEC}s, ${END_SEC}s)"

# ---------------------------------------------------------------------------
# 1. Create the job
# ---------------------------------------------------------------------------

req_body="$(jq -nc \
  --arg url "$SAMPLE_URL" --argjson s "$START_SEC" --argjson e "$END_SEC" \
  '{url: $url, startSec: $s, endSec: $e}')"

create_resp="$(curl --fail-with-body -sS -X POST \
  -H 'Content-Type: application/json' -d "$req_body" \
  "${API_BASE}/v2/jobs")" || { fail "POST /v2/jobs failed: ${create_resp:-<no body>}"; exit 1; }

job_id="$(jq -r '.jobId // empty' <<<"$create_resp")"
if [[ -z "$job_id" ]]; then
  fail "POST /v2/jobs returned no jobId. response: $create_resp"
  exit 1
fi
pass "created job $job_id"

# ---------------------------------------------------------------------------
# 2. Poll until a terminal status or timeout
# ---------------------------------------------------------------------------

deadline=$(( $(date +%s) + TIMEOUT_SEC ))
status=""
last=""
result_url=""

while :; do
  if (( $(date +%s) >= deadline )); then
    fail "timed out after ${TIMEOUT_SEC}s (last status=${status:-none})"
    exit 2
  fi

  poll="$(curl --fail-with-body -sS "${API_BASE}/v2/jobs/${job_id}")" \
    || { fail "GET /v2/jobs/${job_id} failed"; exit 1; }
  status="$(jq -r '.status // "unknown"' <<<"$poll")"

  if [[ "$status" != "$last" ]]; then
    echo "    status → ${status}  (elapsedSec=$(jq -r '.elapsedSec // 0' <<<"$poll"))"
    last="$status"
  fi

  case "$status" in
    done)
      result_url="$(jq -r '.resultUrl // empty' <<<"$poll")"
      break
      ;;
    failed_download|failed_inference|rejected_duration)
      fail "terminal status=${status} error=$(jq -r '.error // "(none)"' <<<"$poll")"
      exit 1
      ;;
    pending|downloading|inferring)
      sleep "$POLL_INTERVAL_SEC"
      ;;
    *)
      echo "    (unexpected status=${status}; continuing to poll)" >&2
      sleep "$POLL_INTERVAL_SEC"
      ;;
  esac
done
pass "job reached status=done"

# ---------------------------------------------------------------------------
# 3. Fetch the result
# ---------------------------------------------------------------------------

if [[ -z "$result_url" ]]; then
  fail "status=done but the response carried no resultUrl"
  exit 1
fi

# S3 serves the result gzip'd (Content-Encoding: gzip), which --compressed
# decodes transparently. If we still don't have JSON, the object is raw gzip
# bytes — re-fetch and gunzip by hand.
result_json="$(curl --fail-with-body --compressed -sS "$result_url" || true)"
if ! jq -e . >/dev/null 2>&1 <<<"$result_json"; then
  tmp="$(mktemp)"
  curl --fail-with-body -sS "$result_url" -o "$tmp" \
    || { fail "could not fetch resultUrl"; rm -f "$tmp"; exit 1; }
  result_json="$(gunzip -c "$tmp" 2>/dev/null || cat "$tmp")"
  rm -f "$tmp"
fi
if ! jq -e . >/dev/null 2>&1 <<<"$result_json"; then
  fail "resultUrl did not return valid JSON"
  exit 1
fi
pass "fetched ActivationPayload"

# ---------------------------------------------------------------------------
# 4. Assert the ActivationPayload shape (CONTRACTS.md §13.3)
# ---------------------------------------------------------------------------

ok=1
mark() { if [[ "$1" == "ok" ]]; then pass "$2"; else fail "$2"; ok=0; fi; }
jqbool() { if jq -e "$1" >/dev/null 2>&1 <<<"$result_json"; then echo ok; else echo no; fi; }

mark "$(jqbool '.videoDurationSec | type == "number"')" "videoDurationSec is a number"
mark "$(jqbool '.modelVersion | type == "string"')"     "modelVersion is a string"

n_ts="$(jq -r '(.timestamps // []) | length' <<<"$result_json")"
mark "$(jqbool '(.timestamps | type == "array") and (.timestamps | length > 0) and (all(.timestamps[]; type == "number"))')" \
  "timestamps is a non-empty number[] (n=${n_ts})"

missing="$(jq -r --argjson exp "$EXPECTED_REGIONS" \
  '($exp - ((.byRegion // {}) | keys)) | join(", ")' <<<"$result_json")"
if [[ -z "$missing" ]]; then mark ok "byRegion has all 8 regions"; else mark no "byRegion missing: ${missing}"; fi

# Every region series must be a number[] of length == timestamps length.
badlen="$(jq -r --argjson exp "$EXPECTED_REGIONS" '
  . as $root
  | ($root.timestamps // [] | length) as $n
  | [ $exp[] as $r
      | ($root.byRegion[$r] // null) as $s
      | if ($s | type) != "array" or any($s[]; type != "number") then "\($r):not-number[]"
        elif ($s | length) != $n then "\($r):len=\($s|length)≠\($n)"
        else empty end ]
  | join(", ")' <<<"$result_json")"
if [[ -z "$badlen" ]]; then
  mark ok "every region series is a number[] of length ${n_ts}"
else
  mark no "series shape problems: ${badlen}"
fi

echo
if [[ "$ok" == "1" ]]; then
  printf '%sPASS%s — single-video pipeline healthy (job %s)\n' "$G" "$Z" "$job_id"
  exit 0
else
  printf '%sFAIL%s — ActivationPayload failed one or more assertions\n' "$R" "$Z" >&2
  exit 1
fi
