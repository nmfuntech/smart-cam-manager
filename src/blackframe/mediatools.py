"""Locate the ffmpeg/ffprobe executables.

``shutil.which`` is not enough on Windows: winget installs ffmpeg in the
*user* PATH, while the BLACKFRAME service runs as LocalSystem and only sees the
system PATH. Without this lookup the service silently skips the H.264
transcode and event clips stay mp4v (not playable in the browser).

Lookup order: ``FFMPEG_BIN``/``FFPROBE_BIN`` env vars, PATH, then the usual
Windows install locations (winget for any user, Chocolatey, Scoop, C:\\ffmpeg).
"""

from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

_IS_WINDOWS = os.name == "nt"

_ENV_KEYS = {"ffmpeg": "FFMPEG_BIN", "ffprobe": "FFPROBE_BIN"}


def _windows_candidates(name: str) -> list[str]:
    exe = f"{name}.exe"
    system_drive = os.environ.get("SystemDrive", "C:")
    users_root = os.path.join(system_drive + os.sep, "Users")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    patterns = [
        os.path.join(users_root, "*", "AppData", "Local", "Microsoft", "WinGet", "Links", exe),
        os.path.join(
            users_root, "*", "AppData", "Local", "Microsoft", "WinGet", "Packages",
            "*FFmpeg*", "*", "bin", exe,
        ),
        os.path.join(users_root, "*", "scoop", "shims", exe),
        os.path.join(program_data, "chocolatey", "bin", exe),
        os.path.join(program_files, "ffmpeg", "bin", exe),
        os.path.join(system_drive + os.sep, "ffmpeg", "bin", exe),
        os.path.join(system_drive + os.sep, "Tools", "ffmpeg", "bin", exe),
    ]
    found: list[str] = []
    for pattern in patterns:
        # Newest build first when several winget versions are installed.
        found.extend(sorted(glob.glob(pattern), reverse=True))
    return found


_found: dict[str, str] = {}


def _cache_clear() -> None:
    _found.clear()


def find_tool(name: str) -> str | None:
    """Absolute path of ``ffmpeg``/``ffprobe``, or None when not installed.

    Only successful lookups are cached: a tool installed after startup is
    picked up on the next call, and a miss never sticks.
    """
    cached = _found.get(name)
    if cached and Path(cached).is_file():
        return cached
    path = _lookup(name)
    if path:
        _found[name] = path
    return path


find_tool.cache_clear = _cache_clear  # type: ignore[attr-defined]


def _lookup(name: str) -> str | None:
    explicit = os.environ.get(_ENV_KEYS.get(name, ""), "").strip().strip('"')
    if explicit and Path(explicit).is_file():
        return explicit
    on_path = shutil.which(name)
    if on_path:
        return on_path
    if _IS_WINDOWS:
        for candidate in _windows_candidates(name):
            if Path(candidate).is_file():
                return candidate
    return None


def ffmpeg_bin() -> str | None:
    return find_tool("ffmpeg")


def ffprobe_bin() -> str | None:
    return find_tool("ffprobe")
