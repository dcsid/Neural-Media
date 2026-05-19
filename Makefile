# Neural Media — common commands.
# All paths assume `make` is invoked from the repo root.

SHELL := /bin/bash
PYTHON ?= python3
PNPM ?= pnpm

REPO_ROOT := $(shell pwd)
DB_PATH ?= $(REPO_ROOT)/data/sqlite/neural_media.db

# Every Python package in this repo (pipeline, api, inference) is exposed
# to children via PYTHONPATH rather than relying on the editable .pth files
# pip drops into site-packages. macOS's sandbox occasionally marks those
# .pth files with the UF_HIDDEN flag, and Python's site.py silently skips
# hidden .pth files — so editable installs vanish from import resolution
# even though `pip list` still claims them. PYTHONPATH bypasses the .pth
# mechanism entirely and is reliable across reboots, reinstalls, and
# sandbox re-scans. The `unhide-pth` target is the manual escape hatch
# for the cases where you're invoking python directly without `make`.
export PYTHONPATH := $(REPO_ROOT)/services/pipeline:$(REPO_ROOT)/services/api:$(REPO_ROOT)/services/inference

.PHONY: help install install-python install-web sample ingest init-db \
        dev dev-api dev-api-mock dev-web test test-python test-web \
        typecheck-web clean unhide-pth bench-ingest e2e

help:
	@echo "Neural Media — make targets"
	@echo ""
	@echo "  make install         install all services and the web app"
	@echo "  make sample          regenerate mock inference outputs (SampleStore)"
	@echo "  make ingest EXPORT=… ingest a real TikTok export through the CLI"
	@echo "  make init-db         create the SQLite catalog (idempotent)"
	@echo "  make dev             run API (:8000) and web (:3000) together"
	@echo "  make dev-api         FastAPI on SqliteStore (demo path — drag-drop visible)"
	@echo "  make dev-api-mock    FastAPI on SampleStore (mock JSON fixtures)"
	@echo "  make dev-web         run only the Next.js dev server"
	@echo "  make test            run all tests"
	@echo "  make typecheck-web   tsc --noEmit for the web app"
	@echo "  make clean           remove generated artifacts (videos, activations, db)"
	@echo "  make unhide-pth      strip macOS UF_HIDDEN from editable .pth files"
	@echo "  make bench-ingest    mock-mode ingest perf (N=5000 or EXPORT=...)"

# -----------------------------------------------------------------------------
# Install
# -----------------------------------------------------------------------------

install: install-python install-web

install-python:
	$(PYTHON) -m pip install -e services/inference
	$(PYTHON) -m pip install -e services/pipeline
	$(PYTHON) -m pip install -e services/api

install-web:
	cd apps/web && $(PNPM) install

# -----------------------------------------------------------------------------
# Sample data
# -----------------------------------------------------------------------------

sample:
	$(PYTHON) services/inference/scripts/build_sample_outputs.py

# -----------------------------------------------------------------------------
# Ingest a real TikTok export.
#
#   make ingest EXPORT=/path/to/user_data.json
#
# Runs the data-pipeline orchestrator end-to-end: importer →
# yt-dlp downloader → ffmpeg preprocessor → ml-inference. Persists into
# `data/sqlite/neural_media.db` and `data/activations/`.
# -----------------------------------------------------------------------------

ingest:
ifndef EXPORT
	$(error EXPORT is required. Usage: make ingest EXPORT=/path/to/user_data.json)
endif
	$(PYTHON) -m neural_media_pipeline $(EXPORT)

# -----------------------------------------------------------------------------
# SQLite catalog bring-up (idempotent).
# -----------------------------------------------------------------------------

init-db:
	$(PYTHON) services/api/scripts/init_db.py

# -----------------------------------------------------------------------------
# Dev
# -----------------------------------------------------------------------------

dev:
	@echo "Run 'make dev-api' and 'make dev-web' in two terminals." && exit 1

# dev-api: SqliteStore by default so drag-and-drop imports show up live on
# the dashboard without restarting the server. The DB is auto-initialised
# on first POST to /api/v1/import — `make init-db` is optional.
# DB_PATH is quoted because the repo path may contain spaces (e.g. "Spring 2026").
dev-api:
	cd services/api && NEURAL_MEDIA_DB_PATH="$(DB_PATH)" \
	  uvicorn neural_media_api.main:app \
	  --host 127.0.0.1 --port 8000 --reload

# dev-api-mock: SampleStore (no env var). Reads `data/sample/mock_inference/`.
# Use this when you want to explore the dashboard with `make sample` data
# instead of the import flow.
dev-api-mock:
	cd services/api && uvicorn neural_media_api.main:app \
	  --host 127.0.0.1 --port 8000 --reload

dev-web:
	cd apps/web && $(PNPM) dev

# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

test: test-python typecheck-web

test-python:
	cd services/inference && $(PYTHON) -m pytest -q
	cd services/pipeline && $(PYTHON) -m pytest -q
	cd services/api && $(PYTHON) -m pytest -q

typecheck-web:
	cd apps/web && $(PNPM) typecheck

# -----------------------------------------------------------------------------
# End-to-end (Playwright)
#
# Walks the full /single → brain user journey against a fully mocked /v2/jobs*
# backend (see tests/e2e/mock-server.ts). Playwright's webServer config boots
# both apps/web (:3000) and the mock (:3001) before the suite runs.
#
# First run requires the chromium browser:
#   $(PNPM) -C tests/e2e exec playwright install chromium
# -----------------------------------------------------------------------------

e2e:
	$(PNPM) -C tests/e2e exec playwright test

# -----------------------------------------------------------------------------
# Clean
# -----------------------------------------------------------------------------

clean:
	rm -rf data/videos/* data/activations/* data/sqlite/*
	@echo "Cleared user data. (Sample fixtures under data/sample/ are kept.)"

# -----------------------------------------------------------------------------
# macOS sandbox helper.
#
# pip occasionally writes editable .pth files into site-packages with the
# UF_HIDDEN BSD flag set. Python's site.py skips hidden .pth files for
# security, which makes editable installs vanish from import resolution.
# This target strips the flag so the editable installs become visible
# again. Idempotent. No-op on non-macOS.
# -----------------------------------------------------------------------------

unhide-pth:
	@if [ "$$(uname -s)" = "Darwin" ]; then \
	  find "$(REPO_ROOT)/.venv-dev/lib" -name '__editable__*.pth' \
	    -exec chflags nohidden {} + 2>/dev/null && \
	  echo "unhid editable .pth files under .venv-dev/lib/"; \
	else \
	  echo "skipped (sandbox UF_HIDDEN issue is macOS-specific)"; \
	fi

# -----------------------------------------------------------------------------
# Mock-mode ingest benchmark.
#
#   make bench-ingest             # uses the user's real Watch History.txt
#   make bench-ingest N=5000      # synthetic N-video run
#   make bench-ingest EXPORT=/path/to/Watch\ History.txt
#
# Reports wall time + videos/sec for one mock-mode end-to-end ingest with
# `--purge-after-inference --purge-activations` (the demo path). See
# docs/bench-results.md for the targets and the numbers we're chasing.
# -----------------------------------------------------------------------------

# Default export = the location the data-pipeline brief calls out. Override
# either EXPORT (real export) or N (synthetic). EXPORT wins when both are set.
EXPORT ?= [redacted-path] Activity/Watch History.txt
N ?=

bench-ingest:
	@if [ -n "$(N)" ]; then \
	  echo "Benchmarking synthetic $(N)-video ingest..."; \
	  $(PYTHON) services/pipeline/scripts/bench_mock.py $(N); \
	elif [ -f "$(EXPORT)" ]; then \
	  echo "Benchmarking real export: $(EXPORT)"; \
	  $(PYTHON) services/pipeline/scripts/bench_mock.py --export "$(EXPORT)"; \
	else \
	  echo "EXPORT=$(EXPORT) not found and N is unset."; \
	  echo "Usage:"; \
	  echo "  make bench-ingest N=5000"; \
	  echo "  make bench-ingest EXPORT=/path/to/Watch\\ History.txt"; \
	  exit 1; \
	fi

# =============================================================================
# Single-video pipeline (v2 cloud architecture).
#
# These targets compose the cloud-mode demo: an HF Space (TRIBE + yt-dlp +
# ffmpeg) does real inference on demand, an AWS API Gateway + Lambda + S3
# stack accepts jobs and serves results, and the Next.js dashboard
# (NEXT_PUBLIC_API_BASE_V2) talks to the gateway. None of this is wired
# into the existing `dev` / `dev-api` flow — those still run the local
# SQLite + SampleStore demo path.
#
# Run `make help-single` for the discovery surface.
# =============================================================================

.PHONY: help-single dev-single dev-single-mock dev-single-api dev-single-web \
        deploy-aws deploy-hf-space smoke-single

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
	@echo "          The bucket name is generated by the template, not a parameter."
	@echo "          See infra/aws/sam-local.md §4 for the sam-env.json contents."
	@echo ""
	@echo "  TERM 3  Next.js web    (port 3000)"
	@echo "    NEXT_PUBLIC_API_BASE_V2=http://127.0.0.1:3001 $(PNPM) --filter @neural-media/web dev"
	@echo ""
	@echo "Then smoke-test: API_BASE=http://127.0.0.1:3001 make smoke-single"
	@echo ""
	@echo "See infra/aws/sam-local.md for the DynamoDB-local / S3-local prereqs."

deploy-aws: ## sam build && sam deploy from infra/aws/
	cd infra/aws && sam build && sam deploy

deploy-hf-space: ## print the recipe to push services/hf-space/ to the HuggingFace remote
	@echo "Deploy the real HF Space (T2's app, not the mock):"
	@echo ""
	@echo "  1. Create a Space on huggingface.co (Docker SDK, GPU/A10G or CPU)."
	@echo "  2. In the Space settings, set environment variables:"
	@echo "       CALLBACK_SHARED_SECRET = <same value you store in SSM at"
	@echo "                                 /neural-media/hf-callback-secret>"
	@echo "  3. From repo root:"
	@echo "       cd services/hf-space"
	@echo "       hf auth login                          # if not already"
	@echo "       git init -b main                       # if not already a repo"
	@echo "       git remote add hf https://huggingface.co/spaces/<user>/<space>"
	@echo "       git add -A && git commit -m 'Deploy single-video Space'"
	@echo "       git push hf main"
	@echo ""
	@echo "  4. The Space rebuilds automatically. Note the public URL"
	@echo "     (e.g. https://<user>-<space>.hf.space) — paste it into"
	@echo "     'sam deploy --guided' as the HFSpaceUrl parameter."
	@echo ""
	@echo "See docs/single-video-deploy.md for the full runbook."

smoke-single: ## run scripts/smoke-test-single.sh against $$API_BASE
	@if [ -z "$$API_BASE" ]; then \
	  echo "ERROR: API_BASE is required. e.g.:"; \
	  echo "  API_BASE=http://127.0.0.1:3001 make smoke-single"; \
	  exit 1; \
	fi
	bash scripts/smoke-test-single.sh
