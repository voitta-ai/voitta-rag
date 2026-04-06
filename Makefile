VENV := .venv
PYTHON := $(VENV)/bin/python3
PIP := $(VENV)/bin/pip3

-include .env

.PHONY: install run docker-build docker-up docker-down

install:
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

run:
	$(PYTHON) -m uvicorn src.voitta.main:app --reload --host 0.0.0.0

docker-build:
	docker compose build

docker-up:
	mkdir -p $(VOITTA_ROOT_PATH)/qdrant $(VOITTA_ROOT_PATH)/fs
	touch $(VOITTA_ROOT_PATH)/voitta.db
	docker compose up -d

docker-down:
	docker compose down
