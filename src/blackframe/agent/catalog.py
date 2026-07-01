"""Prompt di sistema per l'agente, generato dal command registry condiviso.

Nessun elenco comandi duplicato a mano: se un comando viene aggiunto/tolto in
``blackframe.commands.registry`` il prompt cambia automaticamente in modo
coerente. I comandi senza handler eseguibile (es. ``clip``, catalogo-only)
sono esclusi: l'agente non deve mai poterli "suggerire" se non può eseguirli.
"""

from __future__ import annotations

from blackframe.commands import COMMAND_REGISTRY, CommandArgSpec

_SYSTEM_PREAMBLE = (
    "Sei l'assistente vocale di BLACKFRAME, un sistema di videosorveglianza "
    "domestica. L'utente scrive un messaggio libero in italiano. Il tuo unico "
    "compito è capire se corrisponde a uno dei comandi elencati sotto, e a "
    "quali eventuali argomenti."
)

_SYSTEM_INSTRUCTIONS = (
    "Rispondi SOLO con un oggetto JSON, senza altro testo, del tipo:\n"
    '{"command": "<nome-comando-esatto-o-null>", "arg": "<stringa-o-null>"}\n'
    'Usa il valore null per "command" se il messaggio non corrisponde a '
    "nessuno dei comandi elencati. Usa SOLO i nomi comando elencati sotto, "
    "esattamente come scritti: non inventare nomi nuovi."
)


def _describe_arg(spec: CommandArgSpec | None) -> str:
    if spec is None or spec.kind == "none":
        return "nessun argomento"
    if spec.kind == "enum":
        return f"argomento tra: {', '.join(spec.enum)}"
    if spec.kind == "name":
        return "argomento: nome del dispositivo/regola"
    if spec.kind in ("int", "float"):
        return "argomento: numero" + ("" if spec.required else " (opzionale)")
    return "argomento libero"


def build_catalog_text(exclude: frozenset[str] = frozenset()) -> str:
    lines = [
        f"- {spec.name}: {spec.description} ({_describe_arg(spec.arg)})"
        for spec in COMMAND_REGISTRY.values()
        if spec.handler is not None and spec.name not in exclude
    ]
    return "\n".join(lines)


def build_system_prompt(exclude: frozenset[str] = frozenset()) -> str:
    return (
        f"{_SYSTEM_PREAMBLE}\n\n"
        f"Comandi disponibili:\n{build_catalog_text(exclude)}\n\n"
        f"{_SYSTEM_INSTRUCTIONS}"
    )
