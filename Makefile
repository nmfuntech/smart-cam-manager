PYTHON := poetry run python
SETUP_PYTHON := python3
SETUP_FLAGS :=
ifeq ($(MINIMAL),1)
SETUP_FLAGS += --minimal
endif

.PHONY: setup install lock run test clean help

help:
	@echo "Targets:"
	@echo "  make setup    - reset local state, then interactive .env setup with strong key generation"
	@echo "                 use MINIMAL=1 for only required values"
	@echo "  make install  - install dependencies in Poetry venv"
	@echo "  make lock     - refresh poetry.lock"
	@echo "  make run      - start the app on :8000"
	@echo "  make test     - run unit tests"
	@echo "  make clean    - remove local caches and generated local state"

setup:
	$(SETUP_PYTHON) scripts/setup_config.py $(SETUP_FLAGS)

install:
	poetry install

lock:
	poetry lock

run:
	$(PYTHON) app.py

test:
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf __pycache__ .pytest_cache .env data/.camera_profiles.key data/.test-camera-profiles.key data/camera_profiles.json data/camera_profiles.json.unreadable.*.bak captures/motion/*
