"""Hermetic test environment.

The suite must behave the same on a dev Mac, on CI and inside the mini PC
deploy worktree. The real ``.env`` of a machine (Telegram bot enabled, agent,
automation...) must never leak into a test run, so it is not loaded at all,
and every test writes its runtime state files into its own temp folder.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ["BLACKFRAME_SKIP_DOTENV"] = "1"

# Run the whole suite from a throwaway working directory. Many services use
# cwd-relative defaults (data/, captures/, .env): without this, importing the
# app during a test touched the developer's real data/camera_profiles.json.
# Imports keep working because pytest puts src/ and the repo root on sys.path
# as absolute paths. Only config/ (read-only defaults) is copied over.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_sessionstart(session):
    # After pytest resolved testpaths (relative to the repo), before collection.
    workdir = Path(tempfile.mkdtemp(prefix="blackframe-tests-"))
    shutil.copytree(_REPO_ROOT / "config", workdir / "config")
    os.chdir(workdir)
    atexit.register(shutil.rmtree, workdir, True)


@pytest.fixture(autouse=True)
def _isolated_state_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_INVITES_FILE", str(tmp_path / "telegram_invites.json"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("MODE_STATE_PATH", str(tmp_path / "mode.json"))
    monkeypatch.setenv("HEALTH_STATE_PATH", str(tmp_path / "health_state.json"))
    from blackframe import modes

    modes.set_mode_manager(None)
    yield
    modes.set_mode_manager(None)
