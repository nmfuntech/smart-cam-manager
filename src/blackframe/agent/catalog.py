"""Prompt di sistema per l'agente, generato dal command registry condiviso.

Nessun elenco comandi duplicato a mano: se un comando viene aggiunto/tolto in
``blackframe.commands.registry`` il prompt cambia automaticamente in modo
coerente. I comandi senza handler eseguibile (es. ``clip``, catalogo-only)
sono esclusi: l'agente non deve mai poterli "suggerire" se non può eseguirli.
"""

from __future__ import annotations

import json

from blackframe.commands import COMMAND_REGISTRY, CommandArgSpec

_SYSTEM_PREAMBLE = (
    "Sei l'assistente di BLACKFRAME, un sistema di videosorveglianza "
    "domestica. Traduci il messaggio italiano dell'utente in uno dei comandi "
    "elencati sotto."
)

_SYSTEM_INSTRUCTIONS = (
    "Rispondi SOLO con un oggetto JSON, senza altro testo:\n"
    '{"command": "<nome-comando-esatto-o-null>", "arg": "<stringa-o-null>"}\n'
    "Usa SOLO i nomi comando elencati, esattamente come scritti: non "
    'inventare nomi nuovi. Usa "nessuno" come "command" se il messaggio non '
    "corrisponde a nessun comando (saluti, domande generiche, richieste fuori "
    "ambito). Per una domanda sullo stato (dispositivi accesi, eventi "
    "recenti, impostazioni) scegli il comando di lettura che fornisce i "
    "dati: status, devices, events, rules, config."
)

# Sentinella "rifiuto" negli structured outputs: con l'enum vincolato un
# modello piccolo non emette quasi mai il JSON null (la grammatica lo rende
# improbabile) e finirebbe per scegliere un comando a caso per le frasi fuori
# ambito. Una voce stringa dedicata è molto più naturale da generare.
NO_COMMAND_SENTINEL = "nessuno"

# Few-shot come veri turni di chat (user/assistant), non testo nel prompt:
# per un modello 0.5B il chat template è un segnale molto più forte. Mirati
# sugli errori tipici visti a benchmark: domanda di stato -> comando di
# lettura, argomento enum, fuori ambito -> sentinella.
_EXAMPLE_TURNS: tuple[tuple[str, str, str | None], ...] = (
    ("le luci sono accese?", "devices", None),
    ("metti la sensibilita' bassa", "sensitivity", "bassa"),
    ("che tempo fa domani?", NO_COMMAND_SENTINEL, None),
    ("cancella tutte le registrazioni", NO_COMMAND_SENTINEL, None),
)


def build_example_messages() -> list[dict]:
    messages: list[dict] = []
    for user_text, command, arg in _EXAMPLE_TURNS:
        messages.append({"role": "user", "content": user_text})
        messages.append(
            {"role": "assistant", "content": json.dumps({"command": command, "arg": arg})}
        )
    return messages


# Numero massimo di nomi reali (device/regola) inseriti nel prompt: un modello
# piccolo come qwen2.5:0.5b beneficia del grounding sui nomi esistenti, ma un
# elenco troppo lungo satura il contesto senza aggiungere precisione.
_MAX_KNOWN_NAMES = 20


def _describe_arg(
    spec: CommandArgSpec | None, known_names: dict[str, list[str]] | None = None
) -> str:
    if spec is None or spec.kind == "none":
        return "nessun argomento"
    if spec.kind == "enum":
        return f"argomento tra: {', '.join(spec.enum)}"
    if spec.kind == "name":
        names = (known_names or {}).get(spec.name_source or "") if spec.name_source else None
        if names:
            shown = ", ".join(names[:_MAX_KNOWN_NAMES])
            label = "dispositivo" if spec.name_source == "device" else "regola"
            return f"argomento: nome {label} esatto tra: {shown}"
        return "argomento: nome del dispositivo/regola"
    if spec.kind in ("int", "float"):
        return "argomento: numero" + ("" if spec.required else " (opzionale)")
    return "argomento libero"


def build_catalog_text(
    exclude: frozenset[str] = frozenset(), known_names: dict[str, list[str]] | None = None
) -> str:
    lines = [
        f"- {spec.name}: {spec.description} ({_describe_arg(spec.arg, known_names)})"
        for spec in COMMAND_REGISTRY.values()
        if spec.handler is not None and spec.name not in exclude
    ]
    return "\n".join(lines)


def build_system_prompt(
    exclude: frozenset[str] = frozenset(),
    known_names: dict[str, list[str]] | None = None,
) -> str:
    # La sentinella compare anche come voce di catalogo: per un modello
    # piccolo "scegliere il comando nessuno" è molto più naturale che
    # ricordarsi un'istruzione a fondo prompt.
    catalog_text = (
        f"{build_catalog_text(exclude, known_names)}\n"
        f"- {NO_COMMAND_SENTINEL}: il messaggio non corrisponde a nessun comando "
        "(saluti, meteo, domande generiche, richieste fuori ambito)"
    )
    parts = [
        _SYSTEM_PREAMBLE,
        f"Comandi disponibili:\n{catalog_text}",
        _SYSTEM_INSTRUCTIONS,
    ]
    return "\n\n".join(parts)


def build_response_schema(exclude: frozenset[str] = frozenset()) -> dict:
    """JSON Schema per gli structured outputs di Ollama (>= 0.5).

    Vincola la generazione stessa del modello: ``command`` può essere solo
    uno dei nomi eseguibili del registry (o null), quindi un nome inventato
    non può nemmeno essere emesso. La whitelist post-hoc in ``intent`` resta
    come seconda cintura di sicurezza (e copre il fallback ``format: json``
    delle versioni Ollama senza structured outputs).
    """
    names = [
        spec.name
        for spec in COMMAND_REGISTRY.values()
        if spec.handler is not None and spec.name not in exclude
    ]
    names.append(NO_COMMAND_SENTINEL)
    return {
        "type": "object",
        "properties": {
            "command": {"anyOf": [{"type": "string", "enum": names}, {"type": "null"}]},
            "arg": {"type": ["string", "null"]},
        },
        "required": ["command", "arg"],
    }
