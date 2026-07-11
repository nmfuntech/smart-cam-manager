"""Validated, declarative hardware performance profiles."""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from blackframe.runtime_config import RuntimeConfigManager

DEFAULT_CATALOG_PATH = Path("config/performance_profiles.yaml")
DEFAULT_STATE_PATH = Path("data/performance_profile.json")
_PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_PROFILES = 10
_MAX_SETTINGS = 80

# These values are read only while the process/server starts.
RESTART_REQUIRED_KEYS = frozenset(
    {
        "APP_GUNICORN_THREADS",
        "APP_WAITRESS_THREADS",
        "OPENCV_NUM_THREADS",
        "STREAM_MAX_WIDTH",
        "STREAM_JPEG_QUALITY",
        "STREAM_ENCODE_INTERVAL_MS",
    }
)


def detect_hardware() -> dict[str, Any]:
    ram_bytes = 0
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                ram_bytes = int(status.total_physical)
        except (AttributeError, OSError):
            ram_bytes = 0
    else:
        try:
            ram_bytes = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        except (AttributeError, OSError, TypeError, ValueError):
            ram_bytes = 0
    return {
        "ram_gb": round(ram_bytes / (1024**3), 1) if ram_bytes else None,
        "cpu_threads": os.cpu_count() or 1,
        "architecture": platform.machine() or "unknown",
        "platform": platform.system().lower() or "unknown",
    }


class PerformanceProfileManager:
    def __init__(
        self,
        runtime_config: RuntimeConfigManager,
        catalog_path: str | Path = DEFAULT_CATALOG_PATH,
        state_path: str | Path = DEFAULT_STATE_PATH,
        hardware_provider=detect_hardware,
    ) -> None:
        self.runtime_config = runtime_config
        self.catalog_path = Path(catalog_path)
        self.state_path = Path(state_path)
        self._hardware_provider = hardware_provider
        self._lock = threading.Lock()

    def _load_catalog(self) -> dict:
        try:
            if self.catalog_path.stat().st_size > 256 * 1024:
                raise ValueError("Catalogo profili troppo grande")
            payload = yaml.safe_load(self.catalog_path.read_text(encoding="utf-8"))
        except ValueError:
            raise
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Catalogo profili non leggibile: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Catalogo profili: version deve essere 1")
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict) or not 1 <= len(profiles) <= _MAX_PROFILES:
            raise ValueError("Catalogo profili: sezione profiles non valida")
        normalized = {}
        for name, raw in profiles.items():
            normalized[name] = self._validate_profile(name, raw)
        return {"version": 1, "profiles": normalized}

    def _validate_profile(self, name: str, raw: Any) -> dict:
        if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
            raise ValueError(f"Nome profilo non valido: {name!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"Profilo {name}: struttura non valida")
        label = str(raw.get("label") or "").strip()
        description = str(raw.get("description") or "").strip()
        requirements = raw.get("requirements")
        settings = raw.get("settings")
        if not label or len(label) > 100 or len(description) > 300:
            raise ValueError(f"Profilo {name}: label/description non validi")
        if not isinstance(requirements, dict) or not isinstance(settings, dict):
            raise ValueError(f"Profilo {name}: requirements/settings mancanti")
        if not 1 <= len(settings) <= _MAX_SETTINGS:
            raise ValueError(f"Profilo {name}: numero settings non valido")
        allowed_requirements = {
            "min_ram_gb",
            "recommended_ram_gb",
            "min_cpu_threads",
            "max_monitored_cameras",
        }
        if set(requirements) != allowed_requirements:
            raise ValueError(f"Profilo {name}: requirements non validi")
        parsed_requirements = {}
        for key, value in requirements.items():
            try:
                number = float(value) if "ram_gb" in key else int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Profilo {name}: requirement {key} non numerico") from exc
            if number <= 0:
                raise ValueError(f"Profilo {name}: requirement {key} deve essere positivo")
            parsed_requirements[key] = number
        normalized_settings = self.runtime_config.normalize_updates(
            settings,
            allow_internal=True,
        )
        return {
            "name": name,
            "label": label,
            "description": description,
            "requirements": parsed_requirements,
            "settings": normalized_settings,
        }

    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.state_path.parent,
            prefix=f".{self.state_path.name}.",
            delete=False,
        ) as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            temp_path = Path(handle.name)
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.state_path)
        os.chmod(self.state_path, 0o600)

    def _compatibility(self, profile: dict, hardware: dict, camera_count: int) -> dict:
        req = profile["requirements"]
        reasons = []
        ram = hardware.get("ram_gb")
        cpu = hardware.get("cpu_threads") or 1
        if ram is not None and ram < req["min_ram_gb"]:
            reasons.append(f"Richiede almeno {req['min_ram_gb']:g} GB RAM")
        if cpu < req["min_cpu_threads"]:
            reasons.append(f"Richiede almeno {req['min_cpu_threads']} thread CPU")
        if camera_count > req["max_monitored_cameras"]:
            reasons.append(f"Supporta fino a {req['max_monitored_cameras']} camere monitorate")
        return {"compatible": not reasons, "reasons": reasons}

    def _recommend(self, profiles: dict, hardware: dict, camera_count: int) -> str:
        compatible = [
            profile
            for profile in profiles.values()
            if self._compatibility(profile, hardware, camera_count)["compatible"]
        ]
        if not compatible:
            return min(profiles.values(), key=lambda p: p["requirements"]["min_ram_gb"])["name"]
        return max(
            compatible,
            key=lambda p: (
                p["requirements"]["min_ram_gb"],
                p["requirements"]["min_cpu_threads"],
            ),
        )["name"]

    def list_profiles(self, camera_count: int = 1) -> dict:
        catalog = self._load_catalog()
        state = self._load_state()
        hardware = self._hardware_provider()
        profiles = []
        for profile in catalog["profiles"].values():
            profiles.append(
                {
                    key: profile[key]
                    for key in ("name", "label", "description", "requirements")
                }
                | {"compatibility": self._compatibility(profile, hardware, camera_count)}
            )
        active = state.get("profile") if state.get("profile") in catalog["profiles"] else None
        baseline = state.get("applied_values")
        if isinstance(baseline, dict):
            current = self.runtime_config.get_values(baseline)
            overrides = {
                key: current.get(key)
                for key, expected in baseline.items()
                if current.get(key) != expected
            }
        else:
            overrides = {}
        inferred = False
        if active is None:
            for name, profile in catalog["profiles"].items():
                current = self.runtime_config.get_values(profile["settings"])
                if all(current.get(key) == value for key, value in profile["settings"].items()):
                    active = name
                    inferred = True
                    break
        return {
            "profiles": profiles,
            "hardware": hardware | {"monitored_cameras": camera_count},
            "recommended": self._recommend(catalog["profiles"], hardware, camera_count),
            "active": active,
            "inferred": inferred,
            "customized": bool(overrides),
            "overrides": overrides,
            "applied_at": state.get("applied_at"),
        }

    def preview(self, profile_name: str) -> dict:
        catalog = self._load_catalog()
        profile = catalog["profiles"].get(profile_name)
        if profile is None:
            raise ValueError("Profilo prestazioni sconosciuto")
        current = self.runtime_config.get_values(profile["settings"])
        changes = []
        for key, recommended in profile["settings"].items():
            value = current.get(key)
            if value != recommended:
                changes.append(
                    {
                        "key": key,
                        "current": value,
                        "recommended": recommended,
                        "requires_restart": key in RESTART_REQUIRED_KEYS,
                    }
                )
        return {
            "profile": profile_name,
            "label": profile["label"],
            "changes": changes,
            "restart_required": sorted(
                change["key"] for change in changes if change["requires_restart"]
            ),
        }

    def apply(self, profile_name: str) -> dict:
        with self._lock:
            catalog = self._load_catalog()
            profile = catalog["profiles"].get(profile_name)
            if profile is None:
                raise ValueError("Profilo prestazioni sconosciuto")
            preview = self.preview(profile_name)
            settings = profile["settings"]
            previous_env = {key: os.environ.get(key) for key in settings}
            previous_file = (
                self.runtime_config.env_path.read_bytes()
                if self.runtime_config.env_path.exists()
                else None
            )
            try:
                self.runtime_config.update(settings, allow_internal=True)
                self._write_state(
                    {
                        "version": 1,
                        "profile": profile_name,
                        "profile_version": catalog["version"],
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                        "applied_values": settings,
                        "overrides": {},
                    }
                )
            except Exception:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                if previous_file is None:
                    self.runtime_config.env_path.unlink(missing_ok=True)
                else:
                    self.runtime_config.env_path.write_bytes(previous_file)
                    os.chmod(self.runtime_config.env_path, 0o600)
                raise
            return preview | {"updates": settings}

    def record_overrides(self, updates: dict) -> None:
        with self._lock:
            state = self._load_state()
            baseline = state.get("applied_values")
            if not isinstance(baseline, dict):
                return
            overrides = state.get("overrides") if isinstance(state.get("overrides"), dict) else {}
            for key, value in updates.items():
                if key not in baseline:
                    continue
                normalized = self.runtime_config.normalize_updates({key: value})[key]
                if normalized == baseline[key]:
                    overrides.pop(key, None)
                else:
                    overrides[key] = normalized
            state["overrides"] = overrides
            self._write_state(state)


def load_profile_settings(
    profile_name: str,
    catalog_path: str | Path = DEFAULT_CATALOG_PATH,
) -> dict:
    """Load one validated profile for installers and CLI tooling."""
    manager = PerformanceProfileManager(RuntimeConfigManager(), catalog_path=catalog_path)
    catalog = manager._load_catalog()
    profile = catalog["profiles"].get(profile_name)
    if profile is None:
        raise ValueError(f"Profilo prestazioni sconosciuto: {profile_name}")
    return dict(profile["settings"])
