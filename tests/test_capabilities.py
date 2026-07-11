from __future__ import annotations

from dataclasses import dataclass

import pytest

from blackframe.capabilities import (
    ActionDenied,
    ActionRequest,
    ActionResult,
    Capability,
    CapabilityRegistry,
    Entity,
    EntityState,
    ProviderHealth,
    build_services_registry,
)


@dataclass
class _Provider:
    provider_id: str
    entities: list[Entity]

    def discover(self) -> list[Entity]:
        return self.entities

    def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.provider_id, available=True)


class _BrokenProvider:
    provider_id = "broken"

    def discover(self) -> list[Entity]:
        raise RuntimeError("secret details must not escape")

    def health(self) -> ProviderHealth:
        raise RuntimeError("boom")


def test_registry_aggregates_and_sorts_entities() -> None:
    registry = CapabilityRegistry()
    registry.register(
        _Provider(
            "lights",
            [
                Entity("light.z", "Zulu", "light", "lights"),
                Entity("light.a", "Alpha", "light", "lights"),
            ],
        )
    )

    inventory = registry.snapshot()

    assert [item.id for item in inventory.entities] == ["light.a", "light.z"]
    assert inventory.providers[0].available is True


def test_registry_isolates_provider_failure_without_leaking_exception() -> None:
    registry = CapabilityRegistry([_BrokenProvider()])

    inventory = registry.snapshot()

    assert inventory.entities == ()
    assert inventory.providers[0].available is False
    assert inventory.providers[0].error == "provider unavailable"


def test_inventory_public_dict_contains_no_secret_attributes() -> None:
    entity = Entity(
        "light.one",
        "Lampada",
        "light",
        "tuya",
        capabilities=(Capability("power.read", readonly=True),),
        attributes={"room": "studio", "local_key": "do-not-leak", "ip": "10.0.0.2"},
    )
    registry = CapabilityRegistry([_Provider("tuya", [entity])])

    payload = registry.snapshot().to_public_dict()

    assert payload["entities"][0]["attributes"] == {"room": "studio"}
    assert "do-not-leak" not in str(payload)
    assert "10.0.0.2" not in str(payload)


class _CameraProfiles:
    def list_profiles(self):
        return [
            {"id": "front", "name": "Ingresso", "active": True, "monitored": True},
            {"id": "garage", "name": "Garage", "active": False, "monitored": False},
        ]


class _AutomationRegistry:
    def list_devices(self):
        return [
            {
                "name": "lampada_studio",
                "driver": "tuya_lan",
                "local_key": "***",
                "ip": "192.0.2.2",
                "switch_dp": 20,
            }
        ]


class _Features:
    camera_profiles = _CameraProfiles()


class _Services:
    features = _Features()
    automation_registry = _AutomationRegistry()


def test_services_adapters_discover_generic_entities() -> None:
    inventory = build_services_registry(_Services()).snapshot().to_public_dict()

    assert [(e["type"], e["name"]) for e in inventory["entities"]] == [
        ("camera", "Garage"),
        ("camera", "Ingresso"),
        ("light", "lampada_studio"),
    ]
    lamp = next(e for e in inventory["entities"] if e["type"] == "light")
    assert {item["id"] for item in lamp["capabilities"]} == {
        "power.off",
        "power.on",
        "state.read",
        "state.set",
    }
    assert "ip" not in lamp["attributes"]


def test_services_registry_is_reused_for_state_cache() -> None:
    services = _Services()

    assert build_services_registry(services) is build_services_registry(services)


class _ActionProvider:
    provider_id = "actions"

    def __init__(self) -> None:
        self.reads = 0
        self.actions: list[ActionRequest] = []

    def discover(self) -> list[Entity]:
        return [
            Entity(
                "light.office",
                "Office",
                "light",
                self.provider_id,
                capabilities=(
                    Capability("state.read", readonly=True),
                    Capability("power.on", readonly=False, risk="physical"),
                ),
            )
        ]

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, True)

    def read_state(self, entity_id: str) -> EntityState:
        self.reads += 1
        return EntityState(entity_id, "online", {"power": False})

    def execute(self, request: ActionRequest) -> ActionResult:
        self.actions.append(request)
        return ActionResult(True, request.entity_id, request.capability_id, "done")


def test_state_reads_are_cached_by_ttl() -> None:
    provider = _ActionProvider()
    registry = CapabilityRegistry([provider], state_ttl_sec=30)

    first = registry.read_state("light.office")
    second = registry.read_state("light.office")

    assert first == second
    assert provider.reads == 1


def test_physical_action_requires_explicit_confirmation() -> None:
    provider = _ActionProvider()
    registry = CapabilityRegistry([provider])
    request = ActionRequest("light.office", "power.on")

    with pytest.raises(ActionDenied, match="confirmation required"):
        registry.execute(request, confirmed=False)

    assert provider.actions == []


def test_confirmed_action_is_dispatched_to_owning_provider() -> None:
    provider = _ActionProvider()
    registry = CapabilityRegistry([provider])

    result = registry.execute(ActionRequest("light.office", "power.on"), confirmed=True)

    assert result.ok is True
    assert len(provider.actions) == 1


def test_unknown_entity_or_capability_fails_closed() -> None:
    registry = CapabilityRegistry([_ActionProvider()])

    with pytest.raises(ActionDenied, match="unknown entity"):
        registry.execute(ActionRequest("light.missing", "power.on"), confirmed=True)
    with pytest.raises(ActionDenied, match="unsupported capability"):
        registry.execute(ActionRequest("light.office", "shell.run"), confirmed=True)


def test_idempotency_key_prevents_duplicate_physical_action() -> None:
    provider = _ActionProvider()
    registry = CapabilityRegistry([provider])
    request = ActionRequest("light.office", "power.on", request_id="web-session:123")

    first = registry.execute(request, confirmed=True)
    second = registry.execute(request, confirmed=True)

    assert first == second
    assert len(provider.actions) == 1


def test_idempotency_key_cannot_be_reused_for_different_action() -> None:
    provider = _ActionProvider()
    registry = CapabilityRegistry([provider])
    registry.execute(
        ActionRequest("light.office", "power.on", request_id="web-session:123"),
        confirmed=True,
    )

    with pytest.raises(ActionDenied, match="idempotency conflict"):
        registry.execute(
            ActionRequest(
                "light.office",
                "power.on",
                {"different": True},
                request_id="web-session:123",
            ),
            confirmed=True,
        )


def test_action_parameters_have_size_and_secret_guards() -> None:
    registry = CapabilityRegistry([_ActionProvider()])

    with pytest.raises(ActionDenied, match="sensitive parameter"):
        registry.execute(
            ActionRequest("light.office", "power.on", {"token": "secret"}), confirmed=True
        )
    with pytest.raises(ActionDenied, match="parameters too large"):
        registry.execute(
            ActionRequest("light.office", "power.on", {"value": "x" * 5000}),
            confirmed=True,
        )
