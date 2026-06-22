PYTHON := poetry run python
SETUP_PYTHON := python3
SETUP_FLAGS :=
ifeq ($(MINIMAL),1)
SETUP_FLAGS += --minimal
endif

.PHONY: setup install lock run serve hash-password fetch-model test lint lint-fix audit clean bf install-ubuntu help

help:
	@echo "Targets:"
	@echo "  make setup         - reset local state, then interactive .env setup with strong key generation"
	@echo "                      use MINIMAL=1 for only required values"
	@echo "  make install       - install dependencies in Poetry venv"
	@echo "  make lock          - refresh poetry.lock"
	@echo "  make run           - start the dev server on :8000 (Flask)"
	@echo "  make serve         - start the production server (gunicorn, single worker)"
	@echo "  make hash-password - generate an APP_ADMIN_PASSWORD_HASH (prompts for password)"
	@echo "  make fetch-model   - download the MobileNet-SSD detection model into models/"
	@echo "  make test          - run unit tests"
	@echo "  make lint          - run ruff (E/F/I, line-length 100)"
	@echo "  make lint-fix      - run ruff with autofix (import sorting + safe fixes)"
	@echo "  make audit         - SCA: scan dependencies for known CVEs (pip-audit)"
	@echo "  make clean         - remove local caches and generated local state"
	@echo "  make bf ARGS='...' - run the BLACKFRAME CLI (es: make bf ARGS='status')"
	@echo "  make install-ubuntu - installa su Ubuntu (wrappa scripts/install_ubuntu.sh)"

setup:
	$(SETUP_PYTHON) scripts/setup_config.py $(SETUP_FLAGS)

install:
	poetry install

lock:
	poetry lock

run:
	$(PYTHON) app.py

serve:
	poetry run gunicorn -c deploy/gunicorn.conf.py app:app

hash-password:
	@$(PYTHON) -c "from getpass import getpass; from werkzeug.security import generate_password_hash; pw=getpass('Password admin: '); print('\nAggiungi al .env:\nAPP_ADMIN_PASSWORD_HASH='+generate_password_hash(pw))"

fetch-model:
	$(PYTHON) scripts/fetch_model.py

bf:
	$(PYTHON) scripts/bf.py $(ARGS)

install-ubuntu:
	bash scripts/install_ubuntu.sh $(ARGS)

test:
	$(PYTHON) -m pytest -v

lint:
	poetry run ruff check .

lint-fix:
	poetry run ruff check --fix .

audit:
	@poetry run pip-audit --version >/dev/null 2>&1 || poetry run pip install -q pip-audit
	poetry run pip-audit --progress-spinner off

clean:
	@if [ "$(FORCE)" != "1" ] && { [ -f .env ] || [ -f data/.camera_profiles.key ]; }; then \
		printf "WARNING: esistono .env / chiave di cifratura. 'make clean' li cancella e i segreti cifrati diventano irrecuperabili.\n         Conferma con: make clean FORCE=1\n"; \
		exit 1; \
	fi
	rm -rf __pycache__ .pytest_cache .env data/.camera_profiles.key data/.test-camera-profiles.key data/camera_profiles.json data/camera_profiles.json.unreadable.*.bak captures/motion/*
