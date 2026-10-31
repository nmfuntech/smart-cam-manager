"""Make footage and credential files private at startup (POSIX modes / Windows ACLs)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from blackframe.fsperms import IS_WINDOWS, harden_windows_paths

logger = logging.getLogger(__name__)


_PERMS_MARKER_NAME = ".perms_hardened_v1"


def harden_captures_permissions() -> None:
    """Best-effort: make existing footage private (0700 dirs / 0600 files).

    New writes are already private via the process umask; this retro-fixes any
    clips/dirs created before the umask was in effect (e.g. an upgraded install).
    A marker file skips the recursive chmod on later boots: the tree walk grows
    with the archive and on the mini PC costs real boot time for zero gain once
    the retrofit has run (new files are already born private).
    """
    candidates = [
        "captures",
        os.getenv("MOTION_SAVE_DIR", ""),
        os.getenv("CONTINUOUS_RECORD_DIR", ""),
    ]
    if IS_WINDOWS:
        # chmod is a no-op on Windows: restrict the directory ACLs instead.
        harden_windows_paths([Path(c) for c in candidates if c])
        return
    seen: set[Path] = set()
    for base in candidates:
        if not base:
            continue
        try:
            root = Path(base).resolve()
        except OSError:
            continue
        if root in seen or not root.exists():
            continue
        seen.add(root)
        marker = root / _PERMS_MARKER_NAME
        if marker.exists():
            continue
        try:
            os.chmod(root, 0o700)
            for path in root.rglob("*"):
                try:
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
                except OSError:
                    continue
            marker.touch()
            os.chmod(marker, 0o600)
        except OSError:
            logger.warning("Impossibile irrigidire i permessi di %s", root)


def harden_private_runtime_files() -> None:
    """Force mode 0600 on known credential-bearing runtime files."""
    candidates = {
        Path(".env"),
        Path("tinytuya.json"),
        Path("data/tinytuya.json"),
        Path(os.getenv("TELEGRAM_GUESTS_FILE", "data/telegram_guests.json")),
    }
    if IS_WINDOWS:
        # data/ holds the Fernet key, the guest list and the Tuya local keys.
        harden_windows_paths([Path("data"), Path("logs"), *sorted(candidates)])
        return
    for path in candidates:
        if not path.is_file():
            continue
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Impossibile irrigidire i permessi di %s", path)
