"""Registry dei device smart home con segreti cifrati at-rest.

Ricalca il pattern di ``CameraProfileService``: store JSON con i campi sensibili
(``local_key``, ``access_secret``) cifrati via ``ProfileSecretCipher`` (prefisso
``enc::``), keyfile dedicato e migrazione automatica del plaintext alla prima
lettura. Il registry costruisce e mette in cache gli oggetti ``SmartDevice``
risolvendoli per nome logico (gli stessi nomi usati in ``rules.yaml``).
"""

import json
import logging
from pathlib import Path

from automation.devices import DeviceError, SmartDevice, build_device
from service_layer import (
    ProfileSecretCipher,
    _set_private_permissions,
    _write_private_text,
)

logger = logging.getLogger(__name__)

# Campi cifrati nello store device. ``access_secret`` è incluso per compatibilità
# futura con un'eventuale fallback cloud; oggi il driver LAN usa solo ``local_key``.
TUYA_DEVICE_SECRET_FIELDS = ("local_key", "access_secret")

# Campi sensibili redatti nelle viste pubbliche (oltre ai segreti cifrati).
_REDACTED_PLACEHOLDER = "***"


class DeviceRegistry:
    """Carica/salva i device cifrati e li istanzia su richiesta.

    Args:
        store_path: percorso dello store JSON.
        key_path: keyfile Fernet; default ``data/.tuya_devices.key`` accanto allo store.
        device_factory: factory ``dict -> SmartDevice`` (iniettabile nei test per
            usare ``MockDevice`` senza hardware).
    """

    def __init__(
        self,
        store_path: str | Path = "data/tuya_devices.json",
        key_path: str | Path | None = None,
        device_factory=build_device,
    ) -> None:
        self.store_path = Path(store_path)
        default_key_path = self.store_path.with_name(f".{self.store_path.stem}.key")
        self.secret_cipher = ProfileSecretCipher(key_path or default_key_path)
        self._device_factory = device_factory
        self._cache: dict[str, SmartDevice] = {}

    # --- API pubblica -------------------------------------------------------

    def device_names(self) -> list[str]:
        return [d["name"] for d in self._read_store()]

    def list_devices(self) -> list[dict]:
        """Device con i segreti redatti, per viste/API."""
        return [self._sanitize(d) for d in self._read_store()]

    def get_config(self, name: str) -> dict | None:
        """Config del device con i segreti in chiaro (uso interno)."""
        for device in self._read_store():
            if device.get("name") == name:
                return dict(device)
        return None

    def get(self, name: str) -> SmartDevice:
        """Restituisce (e mette in cache) il ``SmartDevice`` per nome logico."""
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        config = self.get_config(name)
        if config is None:
            raise DeviceError(f"Device '{name}' non presente nel registry")
        device = self._device_factory(config)
        self._cache[name] = device
        return device

    def save_device(self, payload: dict) -> dict:
        """Aggiunge/aggiorna un device (upsert per ``name``). Ritorna la vista redatta."""
        device = self._normalize(payload)
        existing = self.get_config(device["name"])
        # Su modifica, un segreto lasciato vuoto conserva il valore precedente
        # (stesso comportamento di CameraProfileService con le password).
        if existing:
            for field in TUYA_DEVICE_SECRET_FIELDS:
                if not device.get(field):
                    device[field] = existing.get(field, "")

        devices = [d for d in self._read_store() if d.get("name") != device["name"]]
        devices.append(device)
        self._write_store(devices)
        self._cache.pop(device["name"], None)
        return self._sanitize(device)

    def delete_device(self, name: str) -> bool:
        devices = self._read_store()
        remaining = [d for d in devices if d.get("name") != name]
        if len(remaining) == len(devices):
            return False
        self._write_store(remaining)
        self._cache.pop(name, None)
        return True

    # --- persistenza --------------------------------------------------------

    def _read_store(self) -> list[dict]:
        if not self.store_path.exists():
            return []
        try:
            raw = json.loads(self.store_path.read_text())
        except json.JSONDecodeError:
            logger.error("Store device Tuya illeggibile (JSON non valido): %s", self.store_path)
            return []
        if not isinstance(raw, list):
            return []

        devices: list[dict] = []
        should_migrate = False
        try:
            for entry in raw:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                device = dict(entry)
                for field in TUYA_DEVICE_SECRET_FIELDS:
                    decrypted, was_plaintext = self.secret_cipher.decrypt(device.get(field, ""))
                    device[field] = decrypted
                    should_migrate = should_migrate or was_plaintext
                devices.append(device)
        except ValueError:
            backup = self._backup_unreadable_store()
            logger.error(
                "Store device Tuya non decifrabile. Backup in %s, store reinizializzato.",
                backup,
            )
            return []

        if should_migrate:
            self._write_store(devices)
        return devices

    def _write_store(self, devices: list[dict]) -> None:
        persisted = []
        for entry in devices:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            device = dict(entry)
            for field in TUYA_DEVICE_SECRET_FIELDS:
                device[field] = self.secret_cipher.encrypt(str(device.get(field, "") or ""))
            persisted.append(device)
        _write_private_text(self.store_path, json.dumps(persisted, indent=2) + "\n")

    def _backup_unreadable_store(self) -> Path:
        import time

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = self.store_path.with_suffix(
            f"{self.store_path.suffix}.unreadable.{timestamp}.bak"
        )
        backup_path.write_bytes(self.store_path.read_bytes())
        _set_private_permissions(backup_path)
        self.store_path.unlink(missing_ok=True)
        return backup_path

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _normalize(payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise DeviceError("Payload device non valido")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise DeviceError("Nome device obbligatorio")
        driver = str(payload.get("driver") or "tuya_lan").strip()
        device = {
            "name": name,
            "driver": driver,
            "device_id": str(payload.get("device_id") or "").strip(),
            "ip": str(payload.get("ip") or "").strip(),
            "local_key": str(payload.get("local_key") or "").strip(),
            "access_secret": str(payload.get("access_secret") or "").strip(),
            "version": float(payload.get("version") or 3.3),
            "switch_dp": int(payload.get("switch_dp") or 1),
        }
        if driver == "tuya_lan" and not (device["device_id"] and device["ip"]):
            raise DeviceError(f"Device Tuya '{name}': device_id e ip obbligatori")
        return device

    @staticmethod
    def _sanitize(device: dict) -> dict:
        rendered = dict(device)
        for field in TUYA_DEVICE_SECRET_FIELDS:
            if rendered.get(field):
                rendered[field] = _REDACTED_PLACEHOLDER
        return rendered
