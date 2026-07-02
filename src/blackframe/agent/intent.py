"""Interpreta testo libero in un comando del registro, con validazione
obbligatoria dell'output del modello prima di qualunque esecuzione.

Il modello Ollama può solo *suggerire* un nome comando + argomento in JSON:
questo modulo verifica che il nome esista davvero nel ``COMMAND_REGISTRY``
(whitelist — un nome inventato dal modello viene sempre rifiutato) e che
l'argomento passi ``validate_arg`` per quel comando. Nessuna fiducia cieca
nell'output dell'LLM, per quanto il prompt lo vincoli a JSON.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from blackframe.automation.rules_store import load_rules_raw
from blackframe.commands import COMMAND_REGISTRY, validate_arg

from . import fastpath, ollama_client
from .catalog import (
    NO_COMMAND_SENTINEL,
    build_example_messages,
    build_response_schema,
    build_system_prompt,
)
from .context import LastTurn

logger = logging.getLogger(__name__)


@dataclass
class Suggestion:
    ok: bool
    command: str | None = None
    arg: str | None = None
    reason: str | None = None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _known_names(services: Any) -> dict[str, list[str]]:
    """Nomi device/regola reali per il grounding del prompt (best-effort)."""
    names: dict[str, list[str]] = {}
    registry = getattr(services, "automation_registry", None) if services is not None else None
    if registry is not None:
        try:
            names["device"] = registry.device_names()
        except Exception:
            logger.exception("Impossibile leggere i nomi device per il grounding del prompt")
    try:
        names["rule"] = [
            r.get("name") for r in load_rules_raw() if isinstance(r, dict) and r.get("name")
        ]
    except Exception:
        logger.exception("Impossibile leggere i nomi regola per il grounding del prompt")
    return names


def interpret(
    text: str,
    exclude: frozenset[str] = frozenset(),
    services: Any = None,
    last_turn: LastTurn | None = None,
) -> Suggestion:
    max_chars = _env_int("AGENT_MAX_INPUT_CHARS", 300)
    text = (text or "").strip()
    if not text:
        return Suggestion(ok=False, reason="Messaggio vuoto.")
    if len(text) > max_chars:
        return Suggestion(ok=False, reason=f"Messaggio troppo lungo (max {max_chars} caratteri).")

    # Fast-path deterministico: le frasi frequenti non pagano l'LLM. La
    # proposta passa comunque da _validate_response come quella del modello.
    if _env_bool("AGENT_FASTPATH", True):
        fast = fastpath.match(text, exclude=exclude, services=services)
        if fast is not None:
            return _validate_response(fast, exclude, services)

    base_url = _env("AGENT_OLLAMA_URL", "http://127.0.0.1:11434")
    model = _env("AGENT_OLLAMA_MODEL", "qwen2.5:0.5b")
    timeout = _env_float("AGENT_TIMEOUT_SEC", 8.0)
    keep_alive = _env("AGENT_OLLAMA_KEEP_ALIVE", "30m")

    # Opzioni di generazione tarate per hardware limitato: num_ctx piccolo
    # riduce RAM e tempo di prefill, num_predict basso taglia le generazioni
    # fuori controllo (l'output atteso è un JSON di due campi).
    options = {
        "temperature": _env_float("AGENT_OLLAMA_TEMPERATURE", 0.0),
        "num_ctx": _env_int("AGENT_OLLAMA_NUM_CTX", 1536),
        "num_predict": _env_int("AGENT_OLLAMA_NUM_PREDICT", 80),
    }
    schema = build_response_schema(exclude) if _env_bool("AGENT_SCHEMA_FORMAT", True) else None

    # Few-shot come turni di chat prima del messaggio reale: segnale molto
    # più forte del testo nel prompt per un modello piccolo.
    history: list[dict] = []
    if _env_bool("AGENT_PROMPT_EXAMPLES", True):
        history.extend(build_example_messages())

    # Contesto conversazionale: l'ultimo turno riuscito come coppia di
    # messaggi reali, subito prima del messaggio corrente, così i follow-up
    # ("ora spegnila") hanno il riferimento. Testo troncato: serve il senso,
    # non il messaggio integrale.
    if last_turn is not None:
        history.append({"role": "user", "content": last_turn.user_text[:120]})
        history.append(
            {
                "role": "assistant",
                "content": json.dumps({"command": last_turn.command, "arg": last_turn.arg}),
            }
        )

    known_names = _known_names(services)
    response = ollama_client.chat_json(
        base_url,
        model,
        build_system_prompt(exclude, known_names),
        text,
        timeout=timeout,
        keep_alive=keep_alive or None,
        history=history,
        response_schema=schema,
        options=options,
    )
    if response is None:
        return Suggestion(ok=False, reason="Assistente non disponibile al momento.")

    return _validate_response(response, exclude, services)


def _validate_response(response: dict, exclude: frozenset[str], services: Any) -> Suggestion:
    """Valida una proposta ``{"command", "arg"}`` — venga essa dall'LLM o dal
    fast-path deterministico: percorso unico, nessuna sorgente può aggirare
    whitelist e ``validate_arg``."""
    command = response.get("command")
    if not command or not isinstance(command, str) or command == NO_COMMAND_SENTINEL:
        return Suggestion(ok=False, reason="Non ho capito, usa /help per i comandi.")

    spec = COMMAND_REGISTRY.get(command)
    if spec is None or spec.handler is None or command in exclude:
        # Whitelist rigorosa: un nome comando non nel registro (o non
        # eseguibile/non disponibile su questo canale, es. "clip"/media su
        # web) viene rifiutato anche se il modello lo propone con sicurezza —
        # non è mai il modello a decidere cosa esiste.
        logger.info("Agente: comando suggerito non valido/eseguibile: %r", command)
        return Suggestion(ok=False, reason="Non ho capito, usa /help per i comandi.")

    raw_arg = response.get("arg")
    try:
        arg = validate_arg(spec.arg, raw_arg if isinstance(raw_arg, str) else None, services)
    except ValueError as exc:
        return Suggestion(ok=False, reason=str(exc))

    return Suggestion(ok=True, command=command, arg=arg)
