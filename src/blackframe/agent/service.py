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
import threading
from dataclasses import dataclass
from typing import Any

from blackframe.commands import COMMAND_REGISTRY, CommandResult
from blackframe.commands import execute as registry_execute
from blackframe.envutil import env_bool as _env_bool

from . import ollama_client
from .answer import compose_answer, looks_like_question
from .context import ConversationContextStore
from .intent import interpret
from .pending import PendingIntentStore

logger = logging.getLogger(__name__)

_WARMUP_LOCK = threading.Lock()
_WARMUP_THREAD: threading.Thread | None = None


def _run_warmup_once(base_url: str, model: str, keep_alive: str | None) -> None:
    global _WARMUP_THREAD
    try:
        ollama_client.warmup(base_url, model, keep_alive=keep_alive)
    finally:
        with _WARMUP_LOCK:
            _WARMUP_THREAD = None

# Comandi che producono media binari (foto/video): niente formato per
# rappresentarli in una risposta JSON della chat web, quindi il canale "web"
# non li propone nemmeno all'LLM (restano disponibili solo via Telegram).
WEB_EXCLUDED_COMMANDS = frozenset({"snapshot", "latest"})


@dataclass
class ProposalResult:
    ok: bool
    executed: bool = False
    readonly: bool = False
    command: str | None = None
    description: str | None = None
    pending_id: str | None = None
    result: CommandResult | None = None
    # Risposta in italiano naturale composta dai dati del comando (solo per
    # domande su comandi readonly); None -> i canali mostrano result.text.
    answer: str | None = None
    error: str | None = None


class AgentService:
    def __init__(
        self,
        services: Any,
        pending_store: PendingIntentStore | None = None,
        context_store: ConversationContextStore | None = None,
    ) -> None:
        self.services = services
        self._pending = pending_store or PendingIntentStore()
        self._context = context_store or ConversationContextStore()

    @property
    def enabled(self) -> bool:
        return _env_bool("AGENT_ENABLED", False)

    def start_warmup(self) -> None:
        """Precarica il modello Ollama in un thread daemon (best-effort).

        Chiamato da ``app._build_agent`` all'avvio e a ogni toggle, MAI dal
        costruttore: i test costruiscono ``AgentService`` direttamente e non
        devono aprire connessioni. Senza warm-up il primo messaggio utente
        paga il caricamento del modello da disco, che sul mini PC supera
        ``AGENT_TIMEOUT_SEC``.
        """
        if not self.enabled or not _env_bool("AGENT_WARMUP", True):
            return
        global _WARMUP_THREAD
        base_url = os.getenv("AGENT_OLLAMA_URL", "http://127.0.0.1:11434").strip()
        model = os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:0.5b").strip()
        keep_alive = os.getenv("AGENT_OLLAMA_KEEP_ALIVE", "30m").strip()
        with _WARMUP_LOCK:
            if _WARMUP_THREAD is not None and _WARMUP_THREAD.is_alive():
                return
            thread = threading.Thread(
                target=_run_warmup_once,
                args=(base_url, model, keep_alive or None),
                name="agent-warmup",
                daemon=True,
            )
            _WARMUP_THREAD = thread
            try:
                thread.start()
            except Exception:
                _WARMUP_THREAD = None
                raise

    def propose(self, text: str, channel: str, channel_key: str) -> ProposalResult:
        if not self.enabled:
            return ProposalResult(ok=False, error="Assistente non abilitato.")

        exclude = WEB_EXCLUDED_COMMANDS if channel == "web" else frozenset()
        last_turn = self._context.get(channel, channel_key)
        suggestion = interpret(text, exclude=exclude, services=self.services, last_turn=last_turn)
        if not suggestion.ok:
            return ProposalResult(ok=False, error=suggestion.reason or "Non ho capito.")

        # Il turno si salva alla proposta, non alla conferma: "accendi X" →
        # "ora spegnila" deve funzionare anche mentre la prima azione è
        # ancora in attesa di conferma. Le interpretazioni fallite non
        # sovrascrivono il contesto buono precedente.
        self._context.set(channel, channel_key, text, suggestion.command, suggestion.arg)

        spec = COMMAND_REGISTRY[suggestion.command]
        if spec.readonly:
            try:
                result = registry_execute(suggestion.command, suggestion.arg, self.services)
            except Exception:
                logger.exception("Esecuzione comando agente fallita: %s", suggestion.command)
                return ProposalResult(ok=False, error="Esecuzione fallita.")
            # Solo per le domande: la seconda chiamata LLM trasforma
            # l'output tecnico in una risposta naturale. Gli imperativi
            # ("stato") non pagano la latenza extra. Fail-open: answer=None
            # -> i canali mostrano result.text come sempre.
            answer = None
            if (
                _env_bool("AGENT_NATURAL_ANSWERS", True)
                and result is not None
                and result.text
                and looks_like_question(text)
            ):
                answer = compose_answer(text, suggestion.command, result.text)
            return ProposalResult(
                ok=True,
                executed=True,
                readonly=True,
                command=suggestion.command,
                description=spec.description,
                result=result,
                answer=answer,
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
