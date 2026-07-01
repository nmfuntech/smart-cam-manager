"""Orchestratore del layer agentico.

Punto unico che applica la policy di conferma (letta una volta, riusata da
Telegram e dalla Web UI) così i due canali non duplicano la logica di quando
eseguire subito un comando suggerito dall'LLM e quando invece serve conferma
umana. Fail-closed come ``_build_automation()`` in ``app.py``: se l'agente è
disabilitato o Ollama non risponde, non si tenta mai un'esecuzione.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from blackframe.commands import COMMAND_REGISTRY, CommandResult
from blackframe.commands import execute as registry_execute

from .intent import interpret
from .pending import PendingIntentStore

logger = logging.getLogger(__name__)

# Comandi che producono media binari (foto/video): niente formato per
# rappresentarli in una risposta JSON della chat web, quindi il canale "web"
# non li propone nemmeno all'LLM (restano disponibili solo via Telegram).
WEB_EXCLUDED_COMMANDS = frozenset({"snapshot", "latest"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ProposalResult:
    ok: bool
    executed: bool = False
    readonly: bool = False
    command: str | None = None
    description: str | None = None
    pending_id: str | None = None
    result: CommandResult | None = None
    error: str | None = None


class AgentService:
    def __init__(self, services: Any, pending_store: PendingIntentStore | None = None) -> None:
        self.services = services
        self._pending = pending_store or PendingIntentStore()

    @property
    def enabled(self) -> bool:
        return _env_bool("AGENT_ENABLED", False)

    def propose(self, text: str, channel: str, channel_key: str) -> ProposalResult:
        if not self.enabled:
            return ProposalResult(ok=False, error="Assistente non abilitato.")

        exclude = WEB_EXCLUDED_COMMANDS if channel == "web" else frozenset()
        suggestion = interpret(text, exclude=exclude)
        if not suggestion.ok:
            return ProposalResult(ok=False, error=suggestion.reason or "Non ho capito.")

        spec = COMMAND_REGISTRY[suggestion.command]
        if spec.readonly:
            try:
                result = registry_execute(suggestion.command, suggestion.arg, self.services)
            except Exception:
                logger.exception("Esecuzione comando agente fallita: %s", suggestion.command)
                return ProposalResult(ok=False, error="Esecuzione fallita.")
            return ProposalResult(
                ok=True,
                executed=True,
                readonly=True,
                command=suggestion.command,
                description=spec.description,
                result=result,
            )

        pending_id = self._pending.create(channel, channel_key, suggestion.command, suggestion.arg)
        return ProposalResult(
            ok=True,
            executed=False,
            readonly=False,
            command=suggestion.command,
            description=spec.description,
            pending_id=pending_id,
        )

    def confirm(self, pending_id: str, channel: str, channel_key: str) -> ProposalResult:
        item = self._pending.pop(pending_id, channel, channel_key)
        if item is None:
            return ProposalResult(ok=False, error="Richiesta scaduta o non trovata.")
        try:
            result = registry_execute(item.command, item.arg, self.services)
        except Exception:
            logger.exception("Esecuzione comando agente (confermato) fallita: %s", item.command)
            return ProposalResult(ok=False, error="Esecuzione fallita.")
        spec = COMMAND_REGISTRY.get(item.command)
        return ProposalResult(
            ok=True,
            executed=True,
            command=item.command,
            description=spec.description if spec else None,
            result=result,
        )

    def cancel(self, pending_id: str, channel: str, channel_key: str) -> bool:
        return self._pending.pop(pending_id, channel, channel_key) is not None
