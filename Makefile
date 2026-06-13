PYTHON := poetry run python
SETUP_PYTHON := python3
SETUP_FLAGS :=
ifeq ($(MINIMAL),1)
SETUP_FLAGS += --minimal
endif

.PHONY: setup install lock run serve hash-password test clean help

help:
	@echo "Targets:"
	@echo "  make setup         - reset local state, then interactive .env setup with strong key generation"
	@echo "                      use MINIMAL=1 for only required values"
	@echo "  make install       - install dependencies in Poetry venv"
	@echo "  make lock          - refresh poetry.lock"
	@echo "  make run           - start the dev server on :8000 (Flask)"
	@echo "  make serve         - start the production server (gunicorn, single worker)"
	@echo "  make hash-password - generate an APP_ADMIN_PASSWORD_HASH (prompts for password)"
	@echo "  make test          - run unit tests"
	@echo "  make clean         - remove local caches and generated local state"

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

test:
	$(PYTHON) -m pytest -v

clean:
	rm -rf __pycache__ .pytest_cache .env data/.camera_profiles.key data/.test-camera-profiles.key data/camera_profiles.json data/camera_profiles.json.unreadable.*.bak captures/motion/*
