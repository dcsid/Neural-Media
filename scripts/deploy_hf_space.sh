#!/usr/bin/env bash
# Deploy the real TRIBE HuggingFace Space from the monorepo, in one command.
#
# The Space's Docker build context needs THREE monorepo trees, not just the
# hf-space dir: services/hf-space/ (the app), services/inference/ (the TRIBE
# wrapper + region masks the Dockerfile `pip install -e`s), and shared/ (the
# contracts package services/inference imports). This script assembles that
# snapshot in a temp dir and force-pushes it to the Space's own HF git repo,
# which triggers HF to rebuild the image. (The Makefile's one-line hint pushes
# only services/hf-space/ and will fail the build — use this instead.)
#
# Prereqs (one-time):
#   - `hf auth login`  (HuggingFace CLI authenticated; provides the git creds)
#   - the Space already exists on huggingface.co (Docker SDK, hardware a10g-small)
#   - the Space has secret CALLBACK_SHARED_SECRET set == the AWS SSM
#     /neural-media/hf-callback-secret value
#
# Usage:
#   scripts/deploy_hf_space.sh <hf-user> [hf-space-name]
#   HF_USER=alice HF_SPACE=neural-media-tribe scripts/deploy_hf_space.sh
#
# Defaults: HF_SPACE=neural-media-tribe. HF_USER is required (arg or env).
set -euo pipefail

HF_USER="${1:-${HF_USER:-}}"
HF_SPACE="${2:-${HF_SPACE:-neural-media-tribe}}"

if [[ -z "$HF_USER" ]]; then
  echo "error: HuggingFace username required." >&2
  echo "  usage: scripts/deploy_hf_space.sh <hf-user> [hf-space-name]" >&2
  echo "  or:    HF_USER=<you> scripts/deploy_hf_space.sh" >&2
  echo "  (run 'hf whoami' to see your username)" >&2
  exit 2
fi

# Always operate from the repo root so the rsync source paths resolve.
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

for tree in services/hf-space services/inference shared; do
  if [[ ! -d "$tree" ]]; then
    echo "error: expected '$tree' under repo root ($REPO_ROOT) — are you in the right repo?" >&2
    exit 1
  fi
done

SPACE_REMOTE="https://huggingface.co/spaces/${HF_USER}/${HF_SPACE}"
SPACE_URL="https://${HF_USER}-${HF_SPACE}.hf.space"   # HF lowercases + joins user/space with '-'

echo "→ staging Space snapshot for ${SPACE_REMOTE}"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

# hf-space files land at the root; inference + shared land as siblings, mirroring
# the monorepo layout the Dockerfile + services/inference/_shared.py expect.
rsync -a --delete services/hf-space/  "$STAGE_DIR/"
mkdir -p "$STAGE_DIR/services"
rsync -a --delete services/inference/ "$STAGE_DIR/services/inference/"
rsync -a --delete shared/             "$STAGE_DIR/shared/"

# Don't ship local caches / build cruft into the image context.
rm -rf "$STAGE_DIR"/__pycache__ "$STAGE_DIR"/.pytest_cache "$STAGE_DIR"/tests/__pycache__ 2>/dev/null || true

cd "$STAGE_DIR"
git init -q
git remote add space "$SPACE_REMOTE"
git add -A
git -c user.email=deploy@neural-media -c user.name=deploy \
    commit -q -m "deploy $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo snapshot)"

echo "→ force-pushing to ${SPACE_REMOTE} (HF will rebuild the image)"
git push -f space HEAD:main

cat <<EOF

✓ Pushed. HF is now rebuilding the Docker image.
  • Watch the build:  ${SPACE_REMOTE}  →  "Logs" / "Building" tab
    (first build after a Dockerfile change is ~10 min; it bakes spaCy etc.)
  • When it shows "Application startup complete", confirm it's up:
      curl -sf ${SPACE_URL}/healthz && echo ' ✓ Space is up'
  • WARM IT ONCE before real users hit it (the first /predict downloads
    the model weights to /data). Run a throwaway upload through the site,
    or hit the smoke test, and let it finish.
EOF
