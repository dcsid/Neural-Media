# Neural Media — common commands.
# All paths assume `make` is invoked from the repo root.

SHELL := /bin/bash
PYTHON ?= python3
PNPM ?= pnpm

.PHONY: help install install-python install-web sample ingest init-db \
        dev dev-api dev-web test test-python test-web typecheck-web clean

help:
	@echo "Neural Media — make targets"
	@echo ""
	@echo "  make install         install all services and the web app"
	@echo "  make sample          regenerate mock inference outputs"
	@echo "  make ingest EXPORT=… ingest a real TikTok export through the pipeline"
	@echo "  make init-db         create the SQLite catalog (idempotent)"
	@echo "  make dev             run API (:8000) and web (:3000) together"
	@echo "  make dev-api         run only the FastAPI service"
	@echo "  make dev-web         run only the Next.js dev server"
	@echo "  make test            run all tests"
	@echo "  make typecheck-web   tsc --noEmit for the web app"
	@echo "  make clean           remove generated artifacts (videos, activations, db)"

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

dev-api:
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
