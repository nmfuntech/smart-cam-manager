"""Import device Tuya da output tinytuya (devices.json / snapshot.json) nel registry."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import yaml

_LOGICAL_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def slugify_smart_name(name: str) -> str:
    """Converte un nome Smart Life in identificatore rules.yaml (a-z, 0-9, _)."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "device"


def load_name_map(path: str | Path | None) -> dict[str, str]:
    """Mappa ``nome Smart Life`` → ``nome logico`` da YAML/JSON."""
    if not path:
        return {}
    map_path = Path(path)
    if not map_path.exists():
        raise FileNotFoundError(f"File mappa nomi non trovato: {map_path}")
    raw = map_path.read_text(encoding="utf-8")
    if map_path.suffix.lower() == ".json":
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("La mappa nomi deve essere un oggetto {nome_smart: nome_logico}")
    result: dict[str, str] = {}
    for smart_name, logical in data.items():
        logical_name = str(logical or "").strip()
        if not _LOGICAL_NAME_RE.fullmatch(logical_name):
            raise ValueError(
                f"Nome logico non valido '{logical_name}' per '{smart_name}': "
                "usa solo lettere minuscole, cifre e underscore"
            )
        result[str(smart_name)] = logical_name
    return result


def load_tinytuya_devices(path: str | Path) -> list[dict]:
    """Legge ``devices.json`` prodotto da ``tinytuya wizard`` / ``scan``."""
    device_path = Path(path)
    if not device_path.exists():
        raise FileNotFoundError(
            f"File device non trovato: {device_path}. "
            "Esegui prima: poetry run python -m tinytuya wizard "
            "&& poetry run python -m tinytuya scan"
        )
    data = json.loads(device_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Formato {device_path} non valido: attesa una lista JSON")
    return [entry for entry in data if isinstance(entry, dict)]


def load_snapshot_by_id(path: str | Path | None) -> dict[str, dict]:
    """Indicizza ``snapshot.json`` per ``device_id`` (DPS per inferire switch_dp)."""
    if not path:
        return {}
    snap_path = Path(path)
    if not snap_path.exists():
        return {}
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    devices = data.get("devices") if isinstance(data, dict) else data
    if not isinstance(devices, list):
        return {}
    indexed: dict[str, dict] = {}
    for entry in devices:
        if isinstance(entry, dict) and entry.get("id"):
            indexed[str(entry["id"])] = entry
    return indexed


def _normalize_dps(raw_dps: dict | None) -> dict | None:
    """Normalizza DPS da snapshot tinytuya (a volte annidati in ``dps.dps``)."""
    if not isinstance(raw_dps, dict):
        return None
    inner = raw_dps.get("dps")
    if isinstance(inner, dict) and inner:
        return {str(key): value for key, value in inner.items()}
    return {str(key): value for key, value in raw_dps.items()}


def infer_switch_dp(dps: dict | None) -> int:
    """Deduce il DP on/off dal payload status (20 lampade RGBCW, 1 prese)."""
    if not dps:
        return 1
    normalized = {str(key): value for key, value in dps.items()}
    if isinstance(normalized.get("20"), bool):
        return 20
    if isinstance(normalized.get("1"), bool):
        return 1
    for key in ("20", "1", "2"):
        if isinstance(normalized.get(key), bool):
            return int(key)
    return 1


def build_registry_payloads(
    scan_devices: list[dict],
    *,
    snapshot_by_id: dict[str, dict] | None = None,
    name_map: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Converte le voci tinytuya in payload per ``DeviceRegistry.save_device``."""
    snapshot_by_id = snapshot_by_id or {}
    name_map = name_map or {}
    payloads: list[dict] = []
    skipped: list[str] = []

    for entry in scan_devices:
        smart_name = str(entry.get("name") or entry.get("id") or "?")
        device_id = str(entry.get("id") or entry.get("device_id") or "").strip()
        ip = str(entry.get("ip") or "").strip()
        local_key = str(entry.get("key") or entry.get("local_key") or "").strip()
        version_raw = entry.get("version")
        version = float(version_raw) if version_raw not in (None, "") else 3.3

        if not device_id:
            skipped.append(f"{smart_name}: device_id mancante")
            continue
        if not ip:
            skipped.append(f"{smart_name}: IP mancante (offline o non scansionato)")
            continue
        if not local_key:
            skipped.append(f"{smart_name}: local_key mancante (esegui il wizard)")
            continue

        logical_name = name_map.get(smart_name) or slugify_smart_name(smart_name)
        if not _LOGICAL_NAME_RE.fullmatch(logical_name):
            skipped.append(f"{smart_name}: nome logico '{logical_name}' non valido")
            continue

        snap = snapshot_by_id.get(device_id, {})
        switch_dp = infer_switch_dp(_normalize_dps(snap.get("dps")))

        payloads.append(
            {
                "name": logical_name,
                "driver": "tuya_lan",
                "device_id": device_id,
                "ip": ip,
                "local_key": local_key,
                "version": version,
                "switch_dp": switch_dp,
            }
        )

    return payloads, skipped
