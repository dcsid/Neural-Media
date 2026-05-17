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
        typecheck-web clean unhide-pth

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
