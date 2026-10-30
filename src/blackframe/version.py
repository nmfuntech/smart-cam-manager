"""Running version, exposed by /health so deploys can verify the new code is live."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_version() -> str:
    """Git commit of the checkout (12 chars), else the package version."""
    if (_REPO_ROOT / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
                capture_output=True,
                timeout=5,
                check=False,
                creationflags=0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0,
            )
            sha = result.stdout.decode(errors="ignore").strip()
            if result.returncode == 0 and sha:
                return sha[:12]
        except Exception:  # version is informational, never fatal
            pass
    try:
        from importlib.metadata import version

        return version("cam")
    except Exception:
        return "unknown"
