# Neural Media — common commands.
# All paths assume `make` is invoked from the repo root.

SHELL := /bin/bash
PYTHON ?= python3
PNPM ?= pnpm

REPO_ROOT := $(shell pwd)

# The Python packages (the pipeline clip-fetcher + inference) are exposed to
# children via PYTHONPATH rather than the editable .pth files — macOS's sandbox
# occasionally hides those and Python's site.py skips hidden .pth files.
# `unhide-pth` is the manual escape hatch when invoking python without `make`.
export PYTHONPATH := $(REPO_ROOT)/services/pipeline:$(REPO_ROOT)/services/inference

.PHONY: help install install-system install-python install-web \
        dev-web test test-python test-web typecheck-web clean unhide-pth e2e

help:
	@echo "Neural Media — make targets"
	@echo ""
	@echo "  make install         install the Python services + the web app"
	@echo "  make install-system  brew install ffmpeg + other non-Python deps"
	@echo "  make dev-web         run the Next.js dev server (:3000)"
	@echo "  make dev-single      print the local single-video (cloud-mode) dev loop"
	@echo "  make test            python + web typecheck + vitest"
	@echo "  make e2e             Playwright end-to-end suite"
	@echo "  make clean           remove generated artifacts"
	@echo "  make unhide-pth      strip macOS UF_HIDDEN from editable .pth files"
	@echo ""
	@echo "  make help-single     the single-video deploy/dev surface"

# -----------------------------------------------------------------------------
# Install
# -----------------------------------------------------------------------------

install: install-python install-web

install-system:
	@if ! command -v brew >/dev/null 2>&1; then \
	  echo "Homebrew not found. Install from https://brew.sh first."; exit 1; \
	fi
	brew install ffmpeg
	@echo "Done. yt-dlp installs via pip (already in the venv via the [dev] extras)."

install-python:
	$(PYTHON) -m pip install -e services/inference
	$(PYTHON) -m pip install -e services/pipeline

install-web:
	cd apps/web && $(PNPM) install

# -----------------------------------------------------------------------------
# Dev / tests
# -----------------------------------------------------------------------------

dev-web:
	cd apps/web && $(PNPM) dev

test: test-python typecheck-web test-web

test-python:
	cd services/inference && $(PYTHON) -m pytest -q
	cd services/pipeline && $(PYTHON) -m pytest -q

typecheck-web:
	cd apps/web && $(PNPM) typecheck

test-web:
	$(PNPM) --filter @neural-media/web test:run

# -----------------------------------------------------------------------------
# End-to-end (Playwright). Walks the single-video → brain journey at `/`
# against a mocked /v2/jobs* backend (tests/e2e/mock-server.ts). The webServer
# config builds + serves apps/web and boots the mock before the suite runs.
# First run needs chromium: $(PNPM) -C tests/e2e exec playwright install chromium
# -----------------------------------------------------------------------------

e2e:
	$(PNPM) -C tests/e2e exec playwright test

clean:
	rm -rf data/videos/* data/activations/* 2>/dev/null || true
	@echo "Cleared generated artifacts."

# macOS sandbox helper (editable .pth UF_HIDDEN). Idempotent; no-op off macOS.
unhide-pth:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
	  find "$(REPO_ROOT)/.venv-dev/lib" -name '__editable__*.pth' \
	    -exec chflags nohidden {} + 2>/dev/null && \
	  echo "unhid editable .pth files under .venv-dev/lib/"; \
	else \
	  echo "skipped (sandbox UF_HIDDEN issue is macOS-specific)"; \
	fi

# =============================================================================
# Single-video pipeline (AWS + HF Space cloud architecture).
# Run `make help-single` for the discovery surface.
# =============================================================================

.PHONY: help-single dev-single deploy-aws deploy-hf-space smoke-single

help-single:
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

dev-single: ## print the three terminal commands for the local single-video loop
	@echo "Three-terminal local dev loop for the single-video pipeline:"
	@echo ""
	@echo "  TERM 1  mock HF Space  (port 8001)"
	@echo "    python services/hf-space/mock_local.py"
	@echo ""
	@echo "  TERM 2  AWS SAM local  (port 3001)"
	@echo "    cd infra/aws && sam local start-api --port 3001 \\"
	@echo "      --env-vars sam-env.json \\"
	@echo "      --parameter-overrides HFSpaceUrl=http://host.docker.internal:8001 \\"
	@echo "                            HFCallbackSecretSsmName=/neural-media/hf-callback-secret \\"
	@echo "                            HFCallbackSecretSsmVersion=1"
	@echo ""
	@echo "    Note: HFSpaceUrl is the base URL only — jobs_worker appends /predict."
	@echo "          See infra/aws/sam-local.md §4 for the sam-env.json contents."
	@echo ""
	@echo "  TERM 3  Next.js web    (port 3000)"
	@echo "    NEXT_PUBLIC_API_BASE_V2=http://127.0.0.1:3001 $(PNPM) --filter @neural-media/web dev"
	@echo ""
	@echo "Then smoke-test: API_BASE=http://127.0.0.1:3001 make smoke-single"

deploy-aws: ## sam build && sam deploy from infra/aws/
	cd infra/aws && sam build && sam deploy

deploy-hf-space: ## print the recipe to push services/hf-space/ to the HuggingFace remote
	@echo "Deploy the real HF Space (app.py, not the mock):"
	@echo ""
	@echo "  1. Create a Space on huggingface.co (Docker SDK, GPU/A10G or CPU)."
	@echo "  2. Set CALLBACK_SHARED_SECRET in the Space (same value as SSM"
	@echo "     /neural-media/hf-callback-secret)."
	@echo "  3. From repo root:"
	@echo "       cd services/hf-space && hf auth login"
	@echo "       git init -b main"
	@echo "       git remote add hf https://huggingface.co/spaces/<user>/<space>"
	@echo "       git add -A && git commit -m 'Deploy single-video Space' && git push hf main"
	@echo ""
	@echo "See docs/single-video-deploy.md for the full runbook."

smoke-single: ## run scripts/smoke-test-single.sh against $$API_BASE
	@if [ -z "$$API_BASE" ]; then \
	  echo "ERROR: API_BASE is required, e.g. API_BASE=http://127.0.0.1:3001 make smoke-single"; \
	  exit 1; \
	fi
	bash scripts/smoke-test-single.sh
