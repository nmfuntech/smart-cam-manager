"""Running version, exposed by /health so deploys can verify the new code is live."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_git_head(root: Path) -> str | None:
    """Resolve HEAD from the .git files directly.

    The Windows service runs as LocalSystem while the checkout belongs to the
    user: git then refuses the repo ("dubious ownership"), so reading the files
    is the reliable way to report the deployed commit.
    """
    git_dir = root / ".git"
    try:
        if git_dir.is_file():  # worktree: "gitdir: <path>"
            git_dir = Path(git_dir.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        common = git_dir
        commondir = git_dir / "commondir"
        if commondir.is_file():
            common = (git_dir / commondir.read_text(encoding="utf-8").strip()).resolve()
        for base in (git_dir, common):
            ref_file = base / ref
            if ref_file.is_file():
                return ref_file.read_text(encoding="utf-8").strip() or None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except (OSError, IndexError):
        return None
    return None


@lru_cache(maxsize=1)
def get_version() -> str:
    """Git commit of the checkout (12 chars), else the package version."""
    sha = _read_git_head(_REPO_ROOT)
    if sha and len(sha) >= 12:
        return sha[:12]
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
