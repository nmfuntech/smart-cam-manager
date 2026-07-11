"""Inventario generico delle entità e capacità disponibili a runtime.

Il layer agente dipende da questi contratti, non da marche, driver o hardware.
Ogni provider traduce un sottosistema applicativo in entità pubbliche redatte.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("blackframe.agent.audit")

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")

_SENSITIVE_ATTRIBUTE_KEYS = frozenset(
    {
        "access_key",
        "access_secret",
        "device_id",
        "host",
        "ip",
        "local_key",
        "password",
        "secret",
        "token",
        "username",
    }
)


@dataclass(frozen=True)
class Capability:
    id: str
    readonly: bool
    risk: str = "read"

    def to_public_dict(self) -> dict[str, object]:
        return {"id": self.id, "readonly": self.readonly, "risk": self.risk}


class ActionDenied(ValueError):
    """Azione rifiutata prima di raggiungere provider o hardware."""


@dataclass(frozen=True)
class ActionRequest:
    entity_id: str
    capability_id: str
    parameters: dict[str, object] = field(default_factory=dict)
    request_id: str | None = None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    entity_id: str
    capability_id: str
    message: str


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    availability: str
    values: dict[str, object] = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, object]:
        safe_values = {
            key: value
            for key, value in self.values.items()
            if key.lower() not in _SENSITIVE_ATTRIBUTE_KEYS
        }
        return {
            "entity_id": self.entity_id,
            "availability": self.availability,
            "values": safe_values,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    type: str
    provider: str
    capabilities: tuple[Capability, ...] = ()
    availability: str = "configured"
    attributes: dict[str, object] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, object]:
        safe_attributes = {
            key: value
            for key, value in self.attributes.items()
            if key.lower() not in _SENSITIVE_ATTRIBUTE_KEYS
        }
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "provider": self.provider,
            "availability": self.availability,
            "capabilities": [item.to_public_dict() for item in self.capabilities],
            "attributes": safe_attributes,
        }


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    available: bool
    error: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "available": self.available,
            "error": self.error,
        }


@runtime_checkable
class CapabilityProvider(Protocol):
    provider_id: str

    def discover(self) -> list[Entity]: ...

    def health(self) -> ProviderHealth: ...

    def read_state(self, entity_id: str) -> EntityState: ...

    def execute(self, request: ActionRequest) -> ActionResult: ...


@dataclass(frozen=True)
class SystemInventory:
    entities: tuple[Entity, ...]
    providers: tuple[ProviderHealth, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "entities": [entity.to_public_dict() for entity in self.entities],
            "providers": [provider.to_public_dict() for provider in self.providers],
        }


class CapabilityRegistry:
    def __init__(
        self,
        providers: list[CapabilityProvider] | None = None,
        *,
        state_ttl_sec: float | None = None,
    ) -> None:
        self._providers: dict[str, CapabilityProvider] = {}
        self._state_ttl = max(
            0.0,
            state_ttl_sec
            if state_ttl_sec is not None
            else float(os.getenv("CAPABILITY_STATE_TTL_SEC", "15")),
        )
        self._state_lock = threading.Lock()
        self._state_cache: dict[str, tuple[float, EntityState]] = {}
        self._action_lock = threading.Lock()
        self._action_cache: dict[str, tuple[float, str, ActionResult]] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: CapabilityProvider) -> None:
        provider_id = str(getattr(provider, "provider_id", "") or "").strip()
        if not provider_id:
            raise ValueError("Capability provider senza id")
        self._providers[provider_id] = provider

    def snapshot(self) -> SystemInventory:
        entities: list[Entity] = []
        health: list[ProviderHealth] = []
        for provider_id, provider in sorted(self._providers.items()):
            try:
                discovered = provider.discover()
                entities.extend(item for item in discovered if isinstance(item, Entity))
                provider_health = provider.health()
                health.append(provider_health)
            except Exception:
                logger.exception("Capability provider non disponibile: %s", provider_id)
                health.append(
                    ProviderHealth(
                        provider_id=provider_id,
                        available=False,
                        error="provider unavailable",
                    )
                )
        entities.sort(key=lambda item: (item.type, item.name.casefold(), item.id))
        return SystemInventory(tuple(entities), tuple(health))

    def _resolve(self, entity_id: str, capability_id: str) -> tuple[CapabilityProvider, Capability]:
        entity = next((item for item in self.snapshot().entities if item.id == entity_id), None)
        if entity is None:
            raise ActionDenied("unknown entity")
        capability = next(
            (item for item in entity.capabilities if item.id == capability_id), None
        )
        if capability is None:
            raise ActionDenied("unsupported capability")
        provider = self._providers.get(entity.provider)
        if provider is None:
            raise ActionDenied("provider unavailable")
        return provider, capability

    def read_state(self, entity_id: str, *, refresh: bool = False) -> EntityState:
        now = time.monotonic()
        with self._state_lock:
            cached = self._state_cache.get(entity_id)
            if not refresh and cached is not None and now - cached[0] <= self._state_ttl:
                return cached[1]
        provider, _ = self._resolve(entity_id, "state.read")
        reader = getattr(provider, "read_state", None)
        if not callable(reader):
            raise ActionDenied("state unavailable")
        try:
            state = reader(entity_id)
        except Exception:
            logger.exception("Capability state read failed: %s", entity_id)
            state = EntityState(entity_id, "unavailable")
        if not isinstance(state, EntityState) or state.entity_id != entity_id:
            raise ActionDenied("invalid provider state")
        with self._state_lock:
            self._state_cache[entity_id] = (now, state)
        return state

    def execute(self, request: ActionRequest, *, confirmed: bool = False) -> ActionResult:
        if not isinstance(request, ActionRequest):
            raise ActionDenied("invalid action request")
        self._validate_request(request)
        fingerprint = json.dumps(
            [request.entity_id, request.capability_id, request.parameters],
            sort_keys=True,
            ensure_ascii=True,
        )
        if request.request_id:
            with self._action_lock:
                cached = self._action_cache.get(request.request_id)
                if cached is not None and time.monotonic() - cached[0] <= 300:
                    if cached[1] != fingerprint:
                        raise ActionDenied("idempotency conflict")
                    return cached[2]
        provider, capability = self._resolve(request.entity_id, request.capability_id)
        if capability.readonly:
            raise ActionDenied("readonly capability")
        if capability.risk in {"physical", "sensitive"} and not confirmed:
            audit_logger.warning(
                "action denied entity=%s capability=%s reason=confirmation_required",
                request.entity_id,
                request.capability_id,
            )
            raise ActionDenied("confirmation required")
        if capability.risk == "forbidden":
            raise ActionDenied("forbidden capability")
        executor = getattr(provider, "execute", None)
        if not callable(executor):
            raise ActionDenied("provider cannot execute")
        try:
            result = executor(request)
        except Exception:
            logger.exception(
                "Capability action failed: %s %s", request.entity_id, request.capability_id
            )
            return ActionResult(
                False,
                request.entity_id,
                request.capability_id,
                "provider action failed",
            )
        if not isinstance(result, ActionResult):
            raise ActionDenied("invalid provider result")
        with self._state_lock:
            self._state_cache.pop(request.entity_id, None)
        if request.request_id:
            with self._action_lock:
                self._action_cache[request.request_id] = (
                    time.monotonic(),
                    fingerprint,
                    result,
                )
                if len(self._action_cache) > 256:
                    oldest = min(self._action_cache, key=lambda key: self._action_cache[key][0])
                    self._action_cache.pop(oldest, None)
        audit_logger.info(
            "action completed entity=%s capability=%s ok=%s",
            request.entity_id,
            request.capability_id,
            result.ok,
        )
        return result

    @staticmethod
    def _validate_request(request: ActionRequest) -> None:
        if not _IDENTIFIER_RE.fullmatch(request.entity_id):
            raise ActionDenied("invalid entity id")
        if not _IDENTIFIER_RE.fullmatch(request.capability_id):
            raise ActionDenied("invalid capability id")
        if request.request_id is not None and not _IDENTIFIER_RE.fullmatch(request.request_id):
            raise ActionDenied("invalid request id")
        if not isinstance(request.parameters, dict) or len(request.parameters) > 32:
            raise ActionDenied("invalid parameters")
        if any(str(key).lower() in _SENSITIVE_ATTRIBUTE_KEYS for key in request.parameters):
            raise ActionDenied("sensitive parameter")
        try:
            encoded = json.dumps(request.parameters, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ActionDenied("invalid parameters") from exc
        if len(encoded) > 4096:
            raise ActionDenied("parameters too large")


class CameraCapabilityProvider:
    provider_id = "blackframe.cameras"

    def __init__(self, services: Any) -> None:
        self._services = services

    def discover(self) -> list[Entity]:
        features = getattr(self._services, "features", None)
        profiles = getattr(features, "camera_profiles", None)
        if profiles is None:
            return []
        entities = []
        for profile in profiles.list_profiles():
            if not isinstance(profile, dict) or not profile.get("id"):
                continue
            profile_id = str(profile["id"])
            monitored = bool(profile.get("monitored") or profile.get("active"))
            capabilities = [Capability("stream.read", readonly=True)]
            if monitored:
                capabilities.append(Capability("state.read", readonly=True))
            if profile.get("active"):
                capabilities.extend(
                    [
                        Capability("snapshot.read", readonly=True),
                        Capability("ptz.move", readonly=False, risk="physical"),
                    ]
                )
            entities.append(
                Entity(
                    id=f"camera.{profile_id}",
                    name=str(profile.get("name") or profile_id),
                    type="camera",
                    provider=self.provider_id,
                    capabilities=tuple(capabilities),
                    availability="monitored" if monitored else "configured",
                    attributes={
                        "active": bool(profile.get("active")),
                        "monitored": monitored,
                    },
                )
            )
        return entities

    def health(self) -> ProviderHealth:
        return ProviderHealth(self.provider_id, available=True)

    def _profile_id(self, entity_id: str) -> str:
        entity = next((item for item in self.discover() if item.id == entity_id), None)
        if entity is None:
            raise ActionDenied("unknown camera")
        return entity_id.removeprefix("camera.")

    def read_state(self, entity_id: str) -> EntityState:
        profile_id = self._profile_id(entity_id)
        camera, _ = self._services.camera_and_motion(profile_id)
        if camera is None:
            return EntityState(entity_id, "configured")
        status = camera.get_status() or {}
        availability = status.get("connection_state") or (
            "online" if status.get("connected") else "offline"
        )
        return EntityState(
            entity_id,
            str(availability),
            {"connected": bool(status.get("connected"))},
        )

    def execute(self, request: ActionRequest) -> ActionResult:
        self._profile_id(request.entity_id)
        if request.capability_id != "ptz.move":
            raise ActionDenied("unsupported camera action")
        direction = str(request.parameters.get("direction") or "")
        if direction not in {"left", "right", "up", "down", "home", "stop"}:
            raise ActionDenied("invalid PTZ direction")
        ptz = getattr(self._services, "ptz", None)
        if ptz is None:
            return ActionResult(False, request.entity_id, request.capability_id, "PTZ unavailable")
        if direction == "home":
            success, error = ptz.home()
        elif direction == "stop":
            success, error = ptz.stop()
        else:
            success, error = ptz.move(direction)
        return ActionResult(
            bool(success),
            request.entity_id,
            request.capability_id,
            "PTZ executed" if success else f"PTZ failed: {error}",
        )


class SmartDeviceCapabilityProvider:
    provider_id = "blackframe.smart_devices"

    def __init__(self, services: Any) -> None:
        self._services = services

    def discover(self) -> list[Entity]:
        registry = getattr(self._services, "automation_registry", None)
        if registry is None:
            return []
        list_devices = getattr(registry, "list_devices", None)
        if callable(list_devices):
            devices = list_devices()
        else:
            devices = [{"name": name} for name in registry.device_names()]
        entities = []
        for device in devices:
            if not isinstance(device, dict) or not device.get("name"):
                continue
            name = str(device["name"])
            driver = str(device.get("driver") or "unknown")
            device_type = str(device.get("type") or "light")
            entities.append(
                Entity(
                    id=f"{device_type}.{name}",
                    name=name,
                    type=device_type,
                    provider=self.provider_id,
                    capabilities=(
                        Capability("state.read", readonly=True),
                        Capability("power.on", readonly=False, risk="physical"),
                        Capability("power.off", readonly=False, risk="physical"),
                        Capability("state.set", readonly=False, risk="physical"),
                    ),
                    attributes={"driver": driver},
                )
            )
        return entities

    def health(self) -> ProviderHealth:
        registry = getattr(self._services, "automation_registry", None)
        return ProviderHealth(self.provider_id, available=registry is not None)

    def _device_name(self, entity_id: str) -> str:
        entity = next((item for item in self.discover() if item.id == entity_id), None)
        if entity is None:
            raise ActionDenied("unknown smart device")
        return entity.name

    def read_state(self, entity_id: str) -> EntityState:
        registry = getattr(self._services, "automation_registry", None)
        if registry is None:
            return EntityState(entity_id, "unavailable")
        device = registry.get(self._device_name(entity_id))
        reader = getattr(device, "get_state", None)
        if not callable(reader):
            return EntityState(entity_id, "unknown")
        values = reader()
        return EntityState(entity_id, "online", values if isinstance(values, dict) else {})

    def execute(self, request: ActionRequest) -> ActionResult:
        registry = getattr(self._services, "automation_registry", None)
        if registry is None:
            return ActionResult(
                False, request.entity_id, request.capability_id, "device unavailable"
            )
        device = registry.get(self._device_name(request.entity_id))
        if request.capability_id == "power.on":
            device.turn_on()
        elif request.capability_id == "power.off":
            device.turn_off()
        elif request.capability_id == "state.set":
            state = request.parameters.get("state")
            if not isinstance(state, dict) or not state or len(state) > 32:
                raise ActionDenied("invalid state payload")
            if not all(str(key).isdigit() for key in state):
                raise ActionDenied("invalid state key")
            device.set_state(state)
        else:
            raise ActionDenied("unsupported smart device action")
        return ActionResult(
            True, request.entity_id, request.capability_id, "device action executed"
        )


def build_services_registry(services: Any) -> CapabilityRegistry:
    """Crea registry dai servizi disponibili senza assumere sistema operativo."""
    existing = getattr(services, "capability_registry", None)
    if isinstance(existing, CapabilityRegistry):
        return existing
    registry = CapabilityRegistry(
        [
            CameraCapabilityProvider(services),
            SmartDeviceCapabilityProvider(services),
        ]
    )
    try:
        services.capability_registry = registry
    except (AttributeError, TypeError):
        pass
    return registry
