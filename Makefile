VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip3
UV := $(VENV)/bin/uv

-include .env

.PHONY: install run models

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install uv
	$(UV) pip install -e ".[dev]"
	$(UV) pip install "mineru[all]" pymupdf
	@echo ""
	@echo "Run 'make models' to download MinerU models (required for PDF parsing)."

models:
	$(VENV)/bin/mineru-models-download

run:
	$(PYTHON) -m uvicorn src.voitta.main:app --reload --host 0.0.0.0 --port $(VOITTA_PORT)
