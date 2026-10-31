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
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from blackframe.automation.rules_store import load_rules_raw
from blackframe.capabilities import build_services_registry
from blackframe.commands import COMMAND_REGISTRY, validate_arg
from blackframe.commands.naming import normalize_identifier
from blackframe.envutil import env_bool as _env_bool
from blackframe.envutil import env_float as _env_float
from blackframe.envutil import env_int as _env_int
from blackframe.envutil import env_str as _env

from . import fastpath, ollama_client, semantic
from .catalog import (
    NO_COMMAND_SENTINEL,
    build_example_messages,
    build_response_schema,
    build_system_prompt,
)
from .context import LastTurn
from .residency import effective_keep_alive

logger = logging.getLogger(__name__)

_LLM_SLOT = threading.BoundedSemaphore(1)
_CACHE_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple, "Suggestion"] = OrderedDict()
_CACHE_MAX = 128

_DOMAIN_TOKENS = frozenset(
    {
        "camera",
        "telecamera",
        "movimento",
        "sensibilita",
        "riconoscimento",
        "persona",
        "persone",
        "animali",
        "cane",
        "gatto",
        "notifiche",
        "silenzia",
        "registrazione",
        "clip",
        "eventi",
        "foto",
        "scatto",
        "dispositivi",
        "device",
        "apparecchi",
        "inventario",
        "luci",
        "regola",
        "regole",
        "automazione",
        "configurazione",
        "impostazioni",
        "stato",
        "ptz",
        "modalita",
        "allarme",
        "armato",
        "disarmato",
        "arma",
        "disarma",
    }
)
_DESTRUCTIVE_TOKENS = frozenset({"cancella", "elimina", "formatta", "distruggi"})

_COMMAND_FAMILIES = {
    "ptz": frozenset({"ptz_left", "ptz_right", "ptz_up", "ptz_down", "ptz_stop", "ptz_home"}),
    "automation": frozenset(
        {
            "inventory",
            "entity_status",
            "devices",
            "device_on",
            "device_off",
            "rules",
            "rule_run",
            "rule_on",
            "rule_off",
        }
    ),
    "motion": frozenset(
        {
            "status",
            "events",
            "motion_on",
            "motion_off",
            "sensitivity",
            "classification_on",
            "classification_off",
            "detect_person_on",
            "detect_person_off",
            "detect_pet_on",
            "detect_pet_off",
            "notifications_on",
            "notifications_off",
            "mute",
            "resume",
            "record_on",
            "record_off",
            "continuous_on",
            "continuous_off",
        }
    ),
}


@dataclass
class Suggestion:
    ok: bool
    command: str | None = None
    arg: str | None = None
    reason: str | None = None
    # Which layer decided: fastpath, semantic, cache, gate, llm (for the log).
    source: str = ""


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
    if services is not None:
        try:
            inventory = build_services_registry(services).snapshot()
            names["entity"] = [entity.name for entity in inventory.entities]
        except Exception:
            logger.exception("Impossibile leggere le entità per il grounding del prompt")
    return names


def _capability_exclude(services: Any, base: frozenset[str]) -> frozenset[str]:
    """Rimuove dal catalogo tool impossibili nella configurazione corrente."""
    if services is None:
        return base
    try:
        entities = build_services_registry(services).snapshot().entities
    except Exception:
        logger.exception("Capability routing non disponibile")
        return base
    capability_ids = {
        capability.id for entity in entities for capability in entity.capabilities
    }
    excluded = set(base)
    if "state.read" not in capability_ids:
        excluded.add("entity_status")
    if not capability_ids & {"power.on", "power.off", "state.set"}:
        excluded.update({"devices", "device_on", "device_off"})
    if "ptz.move" not in capability_ids:
        excluded.update(
            {"ptz_left", "ptz_right", "ptz_up", "ptz_down", "ptz_stop", "ptz_home"}
        )
    return frozenset(excluded)


_FOLLOWUP_TOKENS = frozenset(
    {
        "ora",
        "poi",
        "nuovo",
        "ancora",
        "riattiva",
        "riattivalo",
        "riaccendi",
        "riaccendile",
        "spegnila",
        "accendila",
    }
)


def _semantic_suggestion(
    text: str,
    services: Any,
    known_names: dict[str, list[str]],
    exclude: frozenset[str],
    last_turn: LastTurn | None,
) -> tuple[Suggestion | None, frozenset[str] | None]:
    """Embedding classifier step.

    Returns (suggestion, shortlist): a confident suggestion skips the LLM;
    otherwise the shortlist (commands of the closest topics) narrows the
    LLM's choice and lets the message past the keyword domain gate.
    """
    classifier = semantic.get_classifier()
    if not classifier.enabled():
        return None, None
    if not classifier.ready():
        # Built in the background (warm-up, or a model change from the UI).
        classifier.preload_async()
        return None, None
    tokens = set(normalize_identifier(text).split("_"))
    if tokens & _DESTRUCTIVE_TOKENS:
        return None, None
    # Follow-ups ("ora spegnila") need the previous turn: leave them to the LLM.
    if last_turn is not None and tokens & _FOLLOWUP_TOKENS:
        return None, None
    allowed = {
        name
        for name, spec in COMMAND_REGISTRY.items()
        if spec.handler is not None and name not in exclude
    }
    analysis = classifier.analyze(text, allowed, known_names)
    if analysis is None:
        return None, None
    match = analysis.match
    if match is None:
        return None, analysis.shortlist
    if match.command == semantic.NO_COMMAND:
        rejected = Suggestion(
            ok=False, reason="Richiesta fuori ambito o non supportata.", source="semantic"
        )
        return rejected, None
    result = _validate_response({"command": match.command, "arg": match.arg}, exclude, services)
    if result.ok:
        return replace(result, source="semantic"), None
    return None, analysis.shortlist


def _correct_variant(response: Any, text: str, exclude: frozenset[str]) -> Any:
    """Fix on/off, direction and mode mix-ups of the small LLM.

    When the model picks e.g. ``motion_on`` and the words clearly say "off"
    ("basta rilevamento"), the deterministic resolver of that topic wins. The
    argument (device/rule name) chosen by the model is kept.
    """
    if not isinstance(response, dict):
        return response
    command = response.get("command")
    topic = semantic.TOPIC_OF.get(command) if isinstance(command, str) else None
    if topic is None:
        return response
    fixed = semantic.resolve_in_topic(topic, text)
    if fixed is None or fixed == command or fixed in exclude:
        return response
    logger.info("Agente: %s corretto in %s dalle regole lessicali", command, fixed)
    return {**response, "command": fixed}


def _should_consult_llm(
    text: str, known_names: dict[str, list[str]], last_turn: LastTurn | None
) -> bool:
    """Cheap fail-closed domain gate before expensive inference."""
    normalized = normalize_identifier(text)
    tokens = set(normalized.split("_"))
    if tokens & _DESTRUCTIVE_TOKENS:
        return False
    if last_turn is not None and tokens & _FOLLOWUP_TOKENS:
        return True
    if tokens & _DOMAIN_TOKENS:
        return True
    flattened_names = {
        normalize_identifier(name)
        for values in known_names.values()
        for name in values
        if name
    }
    if any(name and name in normalized for name in flattened_names):
        return True
    known_tokens = {
        token for name in flattened_names for token in name.split("_") if len(token) > 3
    }
    return bool(tokens & known_tokens)


def _cache_get(key: tuple) -> Suggestion | None:
    with _CACHE_LOCK:
        item = _CACHE.pop(key, None)
        if item is not None:
            _CACHE[key] = item
        return item


def _cache_put(key: tuple, value: Suggestion) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def _route_exclude(text: str, base: frozenset[str]) -> frozenset[str]:
    """Reduce prompt/schema only for high-confidence command families."""
    tokens = set(normalize_identifier(text).split("_"))
    family = None
    if tokens & {"sinistra", "destra", "alto", "basso", "giu", "ptz", "inquadra"}:
        family = "ptz"
    elif tokens & {
        "apparecchi",
        "device",
        "dispositivo",
        "dispositivi",
        "inventario",
        "lampada",
        "presa",
        "regola",
        "regole",
    }:
        family = "automation"
    elif tokens & {
        "movimento",
        "sensibilita",
        "riconoscimento",
        "persona",
        "persone",
        "animali",
        "notifiche",
        "registrazione",
        "clip",
    }:
        family = "motion"
    if family is None:
        return base
    allowed = _COMMAND_FAMILIES[family]
    routed_out = {
        name
        for name, spec in COMMAND_REGISTRY.items()
        if spec.handler is not None and name not in allowed
    }
    return frozenset(set(base) | routed_out)


_MUTATING_COMMANDS = frozenset(
    name
    for name, spec in COMMAND_REGISTRY.items()
    if spec.handler is not None and not spec.readonly
)
_READONLY_COMMANDS = frozenset(
    name for name, spec in COMMAND_REGISTRY.items() if spec.handler is not None and spec.readonly
)
# Openings that turn a question into a request ("puoi accendere la luce?").
_REQUEST_OPENERS = frozenset(
    {"puoi", "potresti", "riesci", "vuoi", "mi", "ci", "ti", "per", "dai", "fammi", "fai"}
)
_IMPERATIVE_VERBS = frozenset(
    {
        "accendi",
        "spegni",
        "attiva",
        "disattiva",
        "abilita",
        "disabilita",
        "gira",
        "muovi",
        "sposta",
        "ruota",
        "punta",
        "inquadra",
        "metti",
        "imposta",
        "alza",
        "abbassa",
        "arma",
        "disarma",
        "silenzia",
        "riprendi",
        "ferma",
        "avvia",
        "esegui",
        "registra",
        "torna",
        "ignora",
        "avvisami",
    }
)


def _is_info_question(text: str) -> bool:
    """A question about the state of things ("il cane è uscito?").

    Such a message must never become a state-changing command, so the schema
    is restricted to read-only commands. Polite requests phrased as questions
    ("puoi accendere la luce?", "accendi la luce?") are not info questions.
    """
    if "?" not in text:
        return False
    tokens = normalize_identifier(text).split("_")
    if not tokens or not tokens[0]:
        return False
    first = tokens[0]
    if first in _REQUEST_OPENERS or first in _IMPERATIVE_VERBS:
        return False
    return not (set(tokens[:3]) & _IMPERATIVE_VERBS)


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
        fast = fastpath.match(text, exclude=exclude, services=services, last_turn=last_turn)
        if fast is not None:
            return replace(_validate_response(fast, exclude, services), source="fastpath")

    base_url = _env("AGENT_OLLAMA_URL", "http://127.0.0.1:11434")
    model = _env("AGENT_OLLAMA_MODEL", "qwen2.5:0.5b")
    timeout = _env_float("AGENT_TIMEOUT_SEC", 8.0)
    keep_alive = effective_keep_alive()

    # Opzioni di generazione tarate per hardware limitato: num_ctx piccolo
    # riduce RAM e tempo di prefill, num_predict basso taglia le generazioni
    # fuori controllo (l'output atteso è un JSON di due campi).
    options = {
        "temperature": _env_float("AGENT_OLLAMA_TEMPERATURE", 0.0),
        "num_ctx": _env_int("AGENT_OLLAMA_NUM_CTX", 1536),
        "num_predict": _env_int("AGENT_OLLAMA_NUM_PREDICT", 48),
    }
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
    # The system prompt only depends on the configuration, never on the
    # message: Ollama then reuses the cached prefix (~1.5 s instead of 8-15 s
    # on the N95). Per-message narrowing lives only in the JSON schema enum.
    capability_exclude = _capability_exclude(services, exclude)
    question_exclude = _MUTATING_COMMANDS if _is_info_question(text) else frozenset()

    # Embedding classifier: tens of ms instead of an LLM call when confident.
    sem, short = _semantic_suggestion(
        text, services, known_names, capability_exclude | question_exclude, last_turn
    )
    if sem is not None:
        return sem

    if short is None and _env_bool("AGENT_DOMAIN_GATE", True) and not _should_consult_llm(
        text, known_names, last_turn
    ):
        return Suggestion(
            ok=False, reason="Richiesta fuori ambito o non supportata.", source="gate"
        )

    if short is not None:
        # The classifier already knows the likely topics: the LLM picks among
        # their commands only (a much easier choice for a small model).
        effective_exclude = (
            frozenset(n for n in _MUTATING_COMMANDS | _READONLY_COMMANDS if n not in short)
            | capability_exclude
            | question_exclude
        )
    else:
        effective_exclude = _route_exclude(text, capability_exclude) | question_exclude
    schema = (
        build_response_schema(effective_exclude)
        if _env_bool("AGENT_SCHEMA_FORMAT", True)
        else None
    )

    cache_key = (
        model,
        normalize_identifier(text),
        tuple(sorted(effective_exclude)),
        tuple((kind, tuple(values)) for kind, values in sorted(known_names.items())),
        (last_turn.command, last_turn.arg) if last_turn is not None else None,
    )
    if _env_bool("AGENT_CACHE", True):
        cached = _cache_get(cache_key)
        if cached is not None:
            return replace(cached, source="cache")

    if not _LLM_SLOT.acquire(blocking=False):
        return Suggestion(ok=False, reason="Assistente occupato, riprova tra poco.")
    try:
        response = ollama_client.chat_json(
            base_url,
            model,
            build_system_prompt(capability_exclude, known_names),
            text,
            timeout=timeout,
            keep_alive=keep_alive or None,
            history=history,
            response_schema=schema,
            options=options,
        )
    finally:
        _LLM_SLOT.release()
    if response is None:
        return Suggestion(ok=False, reason="Assistente non disponibile al momento.")

    response = _correct_variant(response, text, effective_exclude)
    result = replace(_validate_response(response, effective_exclude, services), source="llm")
    if result.ok and _env_bool("AGENT_CACHE", True):
        _cache_put(cache_key, result)
    return result


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
