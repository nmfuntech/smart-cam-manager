"""Filesystem privacy helpers that work on both POSIX and Windows.

On POSIX, ``chmod 0600/0700`` is enough. On Windows ``os.chmod`` only toggles
the read-only flag, so it does not protect anything. There, we restrict the
ACL of the *root* directories once instead: inherited entries are converted to
explicit ones, then the broad groups (Users, Authenticated Users, Everyone) are
removed. New files inherit the restricted ACL automatically, so no per-file
subprocess is needed on the hot path (the mini PC is slow at spawning).

Owner, Administrators and SYSTEM keep their access, so neither the Windows
service (LocalSystem) nor the interactive user lose anything. Groups are named
by SID, so the commands work on any Windows language.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

# Well-known SIDs of the groups that must not read private runtime data.
BROAD_GROUP_SIDS = (
    "*S-1-1-0",  # Everyone
    "*S-1-5-11",  # Authenticated Users
    "*S-1-5-32-545",  # BUILTIN\Users
)

WINDOWS_MARKER_NAME = ".perms_hardened_win_v1"

_CREATE_NO_WINDOW = 0x08000000


def _run_icacls(args: list[str]) -> bool:
    try:
        result = subprocess.run(
            ["icacls", *args],
            capture_output=True,
            timeout=60,
            check=False,
            creationflags=_CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("icacls non eseguibile: %s", exc)
        return False
    if result.returncode != 0:
        logger.warning(
            "icacls %s fallito: %s",
            " ".join(args[:2]),
            result.stderr.decode(errors="ignore").strip() or result.returncode,
        )
        return False
    return True


def restrict_windows_acl(path: Path) -> bool:
    """Remove broad-group access from ``path`` (and, by inheritance, its children).

    Returns True when both icacls steps succeeded. Never raises.
    """
    target = str(path)
    if not _run_icacls([target, "/inheritance:d", "/Q"]):
        return False
    return _run_icacls([target, "/remove:g", *BROAD_GROUP_SIDS, "/Q"])


def harden_windows_paths(paths: list[Path]) -> None:
    """Restrict existing directories (once, via marker) and files (every boot)."""
    if not IS_WINDOWS:
        return
    seen: set[Path] = set()
    for raw in paths:
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        if path.is_dir():
            marker = path / WINDOWS_MARKER_NAME
            if marker.exists():
                continue
            if restrict_windows_acl(path):
                try:
                    marker.touch()
                except OSError:
                    pass
        else:
            restrict_windows_acl(path)
