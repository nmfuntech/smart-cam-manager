"""Risoluzione generica del linguaggio naturale verso entità runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from blackframe.capabilities import Entity
from blackframe.commands.naming import normalize_identifier, resolve_name

_GENERIC_ENTITY_TOKENS = {
    "apparecchio",
    "camera",
    "device",
    "dispositivo",
    "lampada",
    "luce",
    "presa",
    "telecamera",
}


@dataclass(frozen=True)
class EntityResolution:
    entity: Entity | None
    suggestions: tuple[str, ...] = ()


def _aliases(entity: Entity) -> set[str]:
    values = {entity.name, entity.id, entity.id.rsplit(".", 1)[-1]}
    values.update(token for token in normalize_identifier(entity.name).split("_") if len(token) > 3)
    room = entity.attributes.get("room")
    if isinstance(room, str) and room:
        values.add(room)
    declared = entity.attributes.get("aliases")
    if isinstance(declared, (list, tuple)):
        values.update(item for item in declared if isinstance(item, str) and item)
    return {normalized for value in values if (normalized := normalize_identifier(value))}


def resolve_entity(
    text: str,
    entities: Iterable[Entity],
    *,
    capability_id: str | None = None,
) -> EntityResolution:
    """Risolve solo corrispondenze univoche; ambiguità resta fail-closed."""
    normalized = normalize_identifier(text)
    if not normalized:
        return EntityResolution(None)
    candidates = [
        entity
        for entity in entities
        if capability_id is None
        or any(capability.id == capability_id for capability in entity.capabilities)
    ]
    if not candidates:
        return EntityResolution(None)

    query_tokens = set(normalized.split("_"))
    contained: list[tuple[int, Entity]] = []
    for entity in candidates:
        matches = []
        for alias in _aliases(entity):
            alias_tokens = set(alias.split("_"))
            if not alias_tokens <= query_tokens:
                continue
            if alias_tokens <= _GENERIC_ENTITY_TOKENS:
                continue
            matches.append(alias)
        if matches:
            contained.append((max(len(alias.split("_")) for alias in matches), entity))
    if contained:
        best_size = max(size for size, _ in contained)
        best = {
            entity.id: entity for size, entity in contained if size == best_size
        }
        if len(best) == 1:
            return EntityResolution(next(iter(best.values())))
        return EntityResolution(None, tuple(sorted(entity.name for entity in best.values())))

    names = [entity.name for entity in candidates]
    resolved, suggestions = resolve_name(normalized, names)
    if resolved is not None:
        entity = next(item for item in candidates if item.name == resolved)
        return EntityResolution(entity)
    return EntityResolution(None, tuple(suggestions))
