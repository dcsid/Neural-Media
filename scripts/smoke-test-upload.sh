#!/usr/bin/env bash
# Post-deploy smoke test for the v2 UPLOAD + segment path.
#
# Drives the full browser chain against a DEPLOYED API base, using a local MP4:
#     POST /v2/jobs/upload { filename, contentType }      -> 201 { jobId, uploadUrl }
#     PUT  uploadUrl  (the raw MP4 bytes, Content-Type video/mp4)
#     POST /v2/jobs/upload/{jobId}/confirm { startSec, endSec }
#     GET  /v2/jobs/{jobId}                                -> poll until terminal
#     GET  resultUrl                                       -> ActivationPayload JSON
# then asserts the ActivationPayload shape (CONTRACTS.md §13.3): all 8 regions
# present, each series a number[] the same length as `timestamps`, and the
# analyzed duration ~= the [startSec, endSec) window we asked the Space to trim.
#
# Usage:
#     scripts/smoke-test-upload.sh <API_BASE> [FILE]
#     FILE=path/to.mp4 START_SEC=0 END_SEC=10 scripts/smoke-test-upload.sh <API_BASE>
#
# Tunables (env): FILE START_SEC END_SEC POLL_INTERVAL_SEC TIMEOUT_SEC
# Deps: curl + jq.
#
# Exit codes: 0 ok · 1 failed/assert · 2 timeout · 3 pre-flight

set -euo pipefail

API_BASE="${1:-${API_BASE:-}}"
FILE="${2:-${FILE:-apps/web/public/demo-clips/ted-ed-why-is-english-so-confusing.mp4}}"
START_SEC="${START_SEC:-0}"
END_SEC="${END_SEC:-10}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-4}"
# Cold-start weight warm-up on the first request after the Space was idle can
# take several minutes; default generously.
TIMEOUT_SEC="${TIMEOUT_SEC:-480}"

EXPECTED_REGIONS='["v1","v2","v3","v4","auditory","language","ffa","vwfa"]'

if [[ -t 1 ]]; then G=$'\033[32m'; R=$'\033[31m'; Z=$'\033[0m'; else G=""; R=""; Z=""; fi
pass() { printf '  %s✓%s %s\n' "$G" "$Z" "$1"; }
fail() { printf '  %s✗%s %s\n' "$R" "$Z" "$1" >&2; }

if [[ -z "$API_BASE" ]]; then
  fail "API base URL required (arg 1 or \$API_BASE)."; exit 3
fi
API_BASE="${API_BASE%/}"
for bin in curl jq; do command -v "$bin" >/dev/null 2>&1 || { fail "\`$bin\` not on PATH."; exit 3; }; done
[[ -f "$FILE" ]] || { fail "upload file not found: $FILE"; exit 3; }

echo "==> API base : $API_BASE"
echo "==> file     : $FILE ($(du -h "$FILE" | cut -f1))  segment [${START_SEC}s, ${END_SEC}s)"

# 1. Mint the upload job + presigned PUT --------------------------------------
create_resp="$(curl --fail-with-body -sS -X POST -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg f "$(basename "$FILE")" --arg ct "video/mp4" '{filename:$f, contentType:$ct}')" \
  "${API_BASE}/v2/jobs/upload")" || { fail "POST /v2/jobs/upload failed: ${create_resp:-<no body>}"; exit 1; }
job_id="$(jq -r '.jobId // empty' <<<"$create_resp")"
upload_url="$(jq -r '.uploadUrl // empty' <<<"$create_resp")"
[[ -n "$job_id" && -n "$upload_url" ]] || { fail "create-upload returned no jobId/uploadUrl: $create_resp"; exit 1; }
pass "created upload job $job_id"

# 2. PUT the bytes to S3 (Content-Type must match the presign: video/mp4) ------
put_code="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT \
  -H 'Content-Type: video/mp4' --upload-file "$FILE" "$upload_url")" || true
[[ "$put_code" == "200" ]] || { fail "S3 PUT returned HTTP ${put_code} (expected 200)"; exit 1; }
pass "uploaded MP4 to S3"

# 3. Confirm with the segment -> kicks the worker ------------------------------
confirm_resp="$(curl --fail-with-body -sS -X POST -H 'Content-Type: application/json' \
  -d "$(jq -nc --argjson s "$START_SEC" --argjson e "$END_SEC" '{startSec:$s, endSec:$e}')" \
  "${API_BASE}/v2/jobs/upload/${job_id}/confirm")" || { fail "confirm failed: ${confirm_resp:-<no body>}"; exit 1; }
pass "confirmed upload + segment"

# 4. Poll until terminal -------------------------------------------------------
deadline=$(( $(date +%s) + TIMEOUT_SEC )); status=""; last=""; result_url=""
while :; do
  (( $(date +%s) >= deadline )) && { fail "timed out after ${TIMEOUT_SEC}s (last=${status:-none})"; exit 2; }
  poll="$(curl --fail-with-body -sS "${API_BASE}/v2/jobs/${job_id}")" || { fail "poll failed"; exit 1; }
  status="$(jq -r '.status // "unknown"' <<<"$poll")"
  if [[ "$status" != "$last" ]]; then
    echo "    status → ${status}  (elapsedSec=$(jq -r '.elapsedSec // 0' <<<"$poll"))"; last="$status"
  fi
  case "$status" in
    done) result_url="$(jq -r '.resultUrl // empty' <<<"$poll")"; break ;;
    failed_download|failed_inference|rejected_duration)
      fail "terminal status=${status} error=$(jq -r '.error // "(none)"' <<<"$poll")"; exit 1 ;;
    *) sleep "$POLL_INTERVAL_SEC" ;;
  esac
done
pass "job reached status=done"

# 5. Fetch + assert the ActivationPayload --------------------------------------
[[ -n "$result_url" ]] || { fail "done but no resultUrl"; exit 1; }
result_json="$(curl --fail-with-body --compressed -sS "$result_url" || true)"
if ! jq -e . >/dev/null 2>&1 <<<"$result_json"; then
  tmp="$(mktemp)"; curl --fail-with-body -sS "$result_url" -o "$tmp" || { fail "fetch resultUrl"; rm -f "$tmp"; exit 1; }
  result_json="$(gunzip -c "$tmp" 2>/dev/null || cat "$tmp")"; rm -f "$tmp"
fi
jq -e . >/dev/null 2>&1 <<<"$result_json" || { fail "resultUrl not valid JSON"; exit 1; }
pass "fetched ActivationPayload"

ok=1
mark() { if [[ "$1" == "ok" ]]; then pass "$2"; else fail "$2"; ok=0; fi; }
jqbool() { if jq -e "$1" >/dev/null 2>&1 <<<"$result_json"; then echo ok; else echo no; fi; }

mark "$(jqbool '.videoDurationSec | type == "number"')" "videoDurationSec is a number"
mark "$(jqbool '.modelVersion | type == "string"')"     "modelVersion is a string"
n_ts="$(jq -r '(.timestamps // []) | length' <<<"$result_json")"
mark "$(jqbool '(.timestamps | type == "array") and (.timestamps | length > 0) and (all(.timestamps[]; type == "number"))')" \
  "timestamps is a non-empty number[] (n=${n_ts})"
missing="$(jq -r --argjson exp "$EXPECTED_REGIONS" '($exp - ((.byRegion // {}) | keys)) | join(", ")' <<<"$result_json")"
[[ -z "$missing" ]] && mark ok "byRegion has all 8 regions" || mark no "byRegion missing: ${missing}"
badlen="$(jq -r --argjson exp "$EXPECTED_REGIONS" '
  . as $root | ($root.timestamps // [] | length) as $n
  | [ $exp[] as $r | ($root.byRegion[$r] // null) as $s
      | if ($s|type) != "array" or any($s[]; type != "number") then "\($r):not-number[]"
        elif ($s|length) != $n then "\($r):len=\($s|length)≠\($n)" else empty end ]
  | join(", ")' <<<"$result_json")"
[[ -z "$badlen" ]] && mark ok "every region series is a number[] of length ${n_ts}" || mark no "series shape: ${badlen}"

# The headline check for THIS path: the Space trimmed to our window, so the
# analyzed duration should be ~ (END_SEC - START_SEC), not the full clip.
want=$(( END_SEC - START_SEC ))
dur="$(jq -r '.videoDurationSec // 0' <<<"$result_json")"
if jq -e --argjson w "$want" '(.videoDurationSec // 0) <= ($w + 3) and (.videoDurationSec // 0) >= ($w - 3)' >/dev/null 2>&1 <<<"$result_json"; then
  mark ok "analyzed duration ${dur}s ~= requested ${want}s window (segment trim worked)"
else
  mark no "analyzed duration ${dur}s != requested ${want}s window (trim may be ignored)"
fi

echo
if [[ "$ok" == "1" ]]; then
  printf '%sPASS%s — upload+segment pipeline healthy (job %s)\n' "$G" "$Z" "$job_id"; exit 0
else
  printf '%sFAIL%s — ActivationPayload failed one or more assertions\n' "$R" "$Z" >&2; exit 1
fi
