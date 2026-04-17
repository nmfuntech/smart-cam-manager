PYTHON := poetry run python

.PHONY: install lock run test clean help

help:
	@echo "Targets:"
	@echo "  make install  - install dependencies in Poetry venv"
	@echo "  make lock     - refresh poetry.lock"
	@echo "  make run      - start the app on :8000"
	@echo "  make test     - run unit tests"
	@echo "  make clean    - remove local caches"

install:
	poetry install

lock:
	poetry lock

run:
	$(PYTHON) app.py

test:
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -rf __pycache__ .pytest_cache
