# Neural Media — common commands.
# All paths assume `make` is invoked from the repo root.

SHELL := /bin/bash
PYTHON ?= python3
PNPM ?= pnpm

.PHONY: help install install-python install-web sample dev dev-api dev-web \
        test test-python test-web typecheck-web clean

help:
	@echo "Neural Media — make targets"
	@echo ""
	@echo "  make install         install all services and the web app"
	@echo "  make sample          regenerate mock inference outputs (needs ml-inference worker)"
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
	$(PYTHON) -m pip install -e services/api

install-web:
	cd apps/web && $(PNPM) install

# -----------------------------------------------------------------------------
# Sample data — owned by ml-inference worker. Calls a script under
# services/inference/scripts that does not exist yet in the scaffold.
# -----------------------------------------------------------------------------

sample:
	$(PYTHON) services/inference/scripts/build_sample_outputs.py

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
	cd services/api && $(PYTHON) -m pytest -q

typecheck-web:
	cd apps/web && $(PNPM) typecheck

# -----------------------------------------------------------------------------
# Clean
# -----------------------------------------------------------------------------

clean:
	rm -rf data/videos/* data/activations/* data/sqlite/*
	@echo "Cleared user data. (Sample fixtures under data/sample/ are kept.)"
