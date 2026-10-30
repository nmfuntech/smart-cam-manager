"""Append-only log of who did what (Telegram commands, agent confirmations, modes).

One JSON object per line in ``data/audit.jsonl``. The file is rotated to
``audit.jsonl.1`` past ``AUDIT_MAX_KB`` so it stays small on the mini PC.
Arguments are stored as given (command names, device names, minutes): never
pass secrets here.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from blackframe.envutil import env_int, env_str

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _audit_path() -> Path:
    return Path(env_str("AUDIT_LOG_PATH", "data/audit.jsonl"))


def record(
    channel: str,
    actor: str,
    action: str,
    arg: str | None = None,
    ok: bool = True,
    detail: str | None = None,
) -> None:
    """Append one entry. Never raises: auditing must not break a command."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": channel,
        "actor": actor,
        "action": action,
    }
    if arg:
        entry["arg"] = str(arg)[:80]
    if not ok:
        entry["ok"] = False
    if detail:
        entry["detail"] = str(detail)[:120]
    path = _audit_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            max_bytes = env_int("AUDIT_MAX_KB", 512, minimum=16) * 1024
            if path.exists() and path.stat().st_size > max_bytes:
                os.replace(path, path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Audit log non scrivibile: %s", path)


def recent(limit: int = 15) -> list[dict]:
    """Most recent entries, newest first."""
    path = _audit_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[dict] = []
    for line in reversed(lines):
        if len(entries) >= limit:
            break
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def format_recent(limit: int = 15) -> str:
    entries = recent(limit)
    if not entries:
        return "Nessuna azione registrata."
    lines = ["🧾 Ultime azioni:"]
    for item in entries:
        ts = str(item.get("ts", "?"))[5:16].replace("T", " ")
        arg = f" {item['arg']}" if item.get("arg") else ""
        mark = " ⚠️" if item.get("ok") is False else ""
        lines.append(f"{ts} · {item.get('actor', '?')} · {item.get('action')}{arg}{mark}")
    return "\n".join(lines)
