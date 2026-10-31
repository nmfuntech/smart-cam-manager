"""Log of the free-text messages the agent interpreted.

Each line of ``data/agent_messages.jsonl`` records what the user wrote, which
command the agent proposed and which layer decided it (fast-path, semantic
classifier, LLM...). Confirm/cancel clicks are logged too: a cancelled
proposal is a strong hint that the interpretation was wrong.

The file exists to build a test set from real messages
(``scripts/export_agent_messages.py``), so the agent is tuned on how the user
actually writes instead of on invented phrases. It stays on the device, under
``data/`` like the rest of the state, and is trimmed to
``AGENT_MESSAGE_LOG_MAX`` lines. ``AGENT_MESSAGE_LOG=false`` disables it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from blackframe.envutil import env_bool, env_int, env_str

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_appends_since_trim = 0
_TRIM_EVERY = 100


def _path() -> Path:
    return Path(env_str("AGENT_MESSAGE_LOG_PATH", "data/agent_messages.jsonl"))


def _trim(path: Path, max_lines: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= max_lines:
        return
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record(event: str, **fields) -> None:
    """Append one record. Never raises: logging must not break the agent."""
    global _appends_since_trim
    if not env_bool("AGENT_MESSAGE_LOG", True):
        return
    entry = {"ts": round(time.time(), 3), "event": event, **fields}
    path = _path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if new_file and os.name != "nt":
                os.chmod(path, 0o600)
            _appends_since_trim += 1
            if _appends_since_trim >= _TRIM_EVERY:
                _appends_since_trim = 0
                _trim(path, env_int("AGENT_MESSAGE_LOG_MAX", 2000, minimum=100))
    except OSError:
        logger.warning("Log messaggi agente non scritto", exc_info=True)


def read_all() -> list[dict]:
    path = _path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries
