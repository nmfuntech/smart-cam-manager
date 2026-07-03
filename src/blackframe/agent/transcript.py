"""Transcript persistente della chat web dell'agente.

La conversazione di ``/agente`` era solo DOM: un reload della pagina la
cancellava. Qui vive su file JSON in ``data/`` (stesso approccio file-based
del resto dell'app), così la history sopravvive a reload e riavvii e la UI
può offrirne la gestione ("Nuova chat").

Scelte deliberate:

- solo canale web — i messaggi Telegram restano nella chat Telegram;
- le proposte in attesa di conferma NON si persistono (vivono nel
  ``PendingIntentStore`` in-memory con TTL): dopo un reload i bottoni
  Conferma/Annulla non tornano, si ripropone il comando;
- rotazione a ``AGENT_TRANSCRIPT_MAX`` messaggi (default 200): chat di un
  singolo utente admin, un tetto basta, niente TTL.

Scrittura atomica identica a ``runtime_config._write_env``: file temporaneo
nella stessa directory, chmod 0600, ``os.replace``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


class AgentTranscriptStore:
    def __init__(
        self,
        path: str | Path | None = None,
        max_messages: int | None = None,
    ) -> None:
        self.path = Path(
            path
            if path is not None
            else os.getenv("AGENT_TRANSCRIPT_PATH", "data/agent_transcript.json")
        )
        self._max = (
            max_messages
            if max_messages is not None
            else max(1, int(os.getenv("AGENT_TRANSCRIPT_MAX", "200") or 200))
        )
        self._lock = threading.Lock()
        self._messages: list[dict] | None = None  # caricamento pigro

    def append(
        self,
        role: str,
        text: str,
        *,
        kind: str = "message",
        command: str | None = None,
    ) -> dict:
        message = {
            "id": uuid.uuid4().hex,
            "role": role,
            "text": text,
            "ts": time.time(),
            "kind": kind,
            "command": command,
        }
        with self._lock:
            messages = self._load_locked()
            messages.append(message)
            del messages[: -self._max]
            self._write_locked(messages)
        return message

    def list(self, limit: int = 100) -> list[dict]:
        limit = max(1, int(limit))
        with self._lock:
            messages = self._load_locked()
            return [dict(item) for item in messages[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._messages = []
            self._write_locked([])

    def _load_locked(self) -> list[dict]:
        if self._messages is not None:
            return self._messages
        messages: list[dict] = []
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                loaded = payload.get("messages") if isinstance(payload, dict) else None
                if isinstance(loaded, list):
                    messages = [item for item in loaded if isinstance(item, dict)]
                else:
                    logger.warning("Transcript agente malformato, reinizializzo: %s", self.path)
            except (json.JSONDecodeError, OSError):
                logger.warning("Transcript agente illeggibile, reinizializzo: %s", self.path)
        self._messages = messages
        return messages

    def _write_locked(self, messages: list[dict]) -> None:
        self._messages = messages
        payload = {"version": _SCHEMA_VERSION, "messages": messages}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                tmp = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            logger.exception("Scrittura transcript agente fallita: %s", self.path)
            if tmp is not None:
                tmp.unlink(missing_ok=True)
