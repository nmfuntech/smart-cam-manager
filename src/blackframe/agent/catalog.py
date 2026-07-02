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
    exclude: frozenset[str] = frozenset(), known_names: dict[str, list[str]] | None = None
) -> str:
    return (
        f"{_SYSTEM_PREAMBLE}\n\n"
        f"Comandi disponibili:\n{build_catalog_text(exclude, known_names)}\n\n"
        f"{_SYSTEM_INSTRUCTIONS}"
    )
