from blackframe.agent.entities import resolve_entity
from blackframe.capabilities import Capability, Entity

ENTITIES = (
    Entity(
        "light.lampada_ingresso",
        "Lampada ingresso",
        "light",
        "test",
        capabilities=(Capability("state.read", True), Capability("power.on", False, "physical")),
        attributes={"room": "ingresso", "aliases": ["luce entrata"]},
    ),
    Entity(
        "light.lampada_studio",
        "Lampada studio",
        "light",
        "test",
        capabilities=(Capability("state.read", True),),
        attributes={"room": "studio"},
    ),
    Entity(
        "camera.front",
        "Camera ingresso",
        "camera",
        "test",
        capabilities=(Capability("state.read", True),),
    ),
)


def test_resolves_name_inside_natural_sentence() -> None:
    result = resolve_entity("qual è lo stato della lampada in ingresso?", ENTITIES)

    assert result.entity is not None
    assert result.entity.id == "light.lampada_ingresso"


def test_resolves_declared_alias() -> None:
    result = resolve_entity("controlla la luce entrata", ENTITIES)

    assert result.entity is not None
    assert result.entity.id == "light.lampada_ingresso"


def test_capability_filter_excludes_incompatible_entity() -> None:
    result = resolve_entity("accendi lampada studio", ENTITIES, capability_id="power.on")

    assert result.entity is None


def test_ambiguous_room_returns_suggestions_without_guessing() -> None:
    result = resolve_entity("ingresso", ENTITIES)

    assert result.entity is None
    assert set(result.suggestions) == {"Camera ingresso", "Lampada ingresso"}
