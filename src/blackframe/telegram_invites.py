"""One-time, expiring Telegram invite codes.

The old ``TELEGRAM_INVITE_CODE`` was static, reusable and never expired: a
leaked link meant permanent camera access. ``/invite`` now mints a random code
that works once and expires (``TELEGRAM_INVITE_TTL_HOURS``, default 24).
Only the SHA-256 of each code is stored, so the file never holds usable codes.

Roles:
- ``famiglia``: read-only commands including photos/clips of the camera
- ``stato``: read-only commands without any image (status, events list...)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from blackframe.envutil import env_float, env_str

ROLE_FAMILY = "famiglia"
ROLE_STATUS = "stato"
ROLES = (ROLE_FAMILY, ROLE_STATUS)

# Commands that send camera images: denied to the "stato" role.
MEDIA_COMMANDS = frozenset({"snapshot", "latest", "clip"})

_lock = threading.Lock()


def _store_path() -> Path:
    return Path(env_str("TELEGRAM_INVITES_FILE", "data/telegram_invites.json"))


def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def _load() -> dict[str, dict]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict]) -> None:
    from blackframe.service_layer import _write_private_text

    _write_private_text(_store_path(), json.dumps(data, indent=2))


def _prune(data: dict[str, dict], now: float) -> dict[str, dict]:
    return {k: v for k, v in data.items() if float(v.get("expires_at", 0)) > now}


def create_invite(role: str = ROLE_FAMILY, created_by: str = "") -> tuple[str, float]:
    """Mint a new code. Returns (code, expires_at_epoch)."""
    if role not in ROLES:
        raise ValueError(f"Ruolo sconosciuto: {role}")
    ttl_hours = env_float("TELEGRAM_INVITE_TTL_HOURS", 24.0, minimum=0.1)
    code = secrets.token_urlsafe(9)
    now = time.time()
    expires_at = now + ttl_hours * 3600
    with _lock:
        data = _prune(_load(), now)
        data[_digest(code)] = {
            "role": role,
            "created_by": created_by,
            "created_at": now,
            "expires_at": expires_at,
        }
        _save(data)
    return code, expires_at


def redeem_invite(code: str) -> str | None:
    """Consume a code. Returns its role, or None if unknown/expired/used."""
    if not code or len(code) > 64:
        return None
    key = _digest(code)
    now = time.time()
    with _lock:
        data = _load()
        entry = data.pop(key, None)
        data = _prune(data, now)
        _save(data)
    if entry is None or float(entry.get("expires_at", 0)) <= now:
        return None
    role = entry.get("role")
    return role if role in ROLES else ROLE_FAMILY


def pending_count() -> int:
    with _lock:
        return len(_prune(_load(), time.time()))
