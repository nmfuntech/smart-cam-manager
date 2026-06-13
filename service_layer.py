import base64
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)
PRIVATE_FILE_MODE = 0o600
CAMERA_PROFILE_SECRET_FIELDS = ("password", "onvif_password")
ENCRYPTED_VALUE_PREFIX = "enc::"


def _set_private_permissions(path: Path) -> None:
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        logger.warning("Impossibile impostare permessi privati su %s", path)


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    _set_private_permissions(temp_path)
    temp_path.replace(path)
    _set_private_permissions(path)


def _write_private_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        delete=False,
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    _set_private_permissions(temp_path)
    temp_path.replace(path)
    _set_private_permissions(path)


class ProfileSecretCipher:
    def __init__(self, key_path: str | Path):
        self.key_path = Path(key_path)
        self.explicit_key = os.getenv("APP_PROFILE_ENCRYPTION_KEY", "").strip()
        self.fernet = Fernet(self._load_or_create_primary_key())
        self.legacy_fernets = self._build_legacy_fernets()

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        token = self.fernet.encrypt(value.encode("utf-8")).decode("utf-8")
        return f"{ENCRYPTED_VALUE_PREFIX}{token}"

    def decrypt(self, value: str) -> tuple[str, bool]:
        text = str(value or "")
        if not text:
            return "", False
        if not text.startswith(ENCRYPTED_VALUE_PREFIX):
            return text, True
        token = text[len(ENCRYPTED_VALUE_PREFIX) :].encode("utf-8")
        try:
            decrypted = self.fernet.decrypt(token).decode("utf-8")
            return decrypted, False
        except InvalidToken as exc:
            for legacy_fernet in self.legacy_fernets:
                try:
                    decrypted = legacy_fernet.decrypt(token).decode("utf-8")
                    return decrypted, True
                except InvalidToken:
                    continue
            raise ValueError("Impossibile decifrare archivio credenziali camera") from exc

    def _load_or_create_primary_key(self) -> bytes:
        if self.explicit_key:
            return self.explicit_key.encode("utf-8")

        if self.key_path.exists():
            key = self.key_path.read_bytes().strip()
            _set_private_permissions(self.key_path)
            return key

        key = Fernet.generate_key()
        _write_private_bytes(self.key_path, key + b"\n")
        return key

    def _build_legacy_fernets(self) -> list[Fernet]:
        legacy_fernets: list[Fernet] = []
        if self.explicit_key and self.key_path.exists():
            file_key = self.key_path.read_bytes().strip()
            _set_private_permissions(self.key_path)
            if file_key and file_key != self.explicit_key.encode("utf-8"):
                legacy_fernets.append(Fernet(file_key))
        app_secret = os.getenv("APP_SECRET_KEY", "").strip()
        if app_secret:
            digest = hashlib.sha256(app_secret.encode("utf-8")).digest()
            legacy_key = base64.urlsafe_b64encode(digest)
            legacy_fernets.append(Fernet(legacy_key))
        return legacy_fernets


class PresetService:
    def __init__(self, store_path: str = "data/presets.json"):
        self.store_path = Path(store_path)

    def list_presets(self) -> list[dict]:
        if not self.store_path.exists():
            return []
        try:
            return json.loads(self.store_path.read_text())
        except json.JSONDecodeError:
            return []

    def save_preset(self, preset: dict) -> dict:
        presets = self.list_presets()
        presets = [item for item in presets if item.get("id") != preset.get("id")]
        presets.append(preset)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(json.dumps(presets, indent=2))
        return preset


class NotificationService:
    def __init__(self):
        self._events: list[dict] = []

    def notify(self, event_type: str, payload: dict) -> dict:
        event = {"type": event_type, "payload": payload}
        self._events.append(event)
        return event

    def recent(self, limit: int = 20) -> list[dict]:
        return self._events[-limit:]


class RecordingService:
    def __init__(self, base_dir: str = "captures/recordings"):
        self.base_dir = Path(base_dir)

    def build_recording_path(self, name: str) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char for char in name if char.isalnum() or char in ("-", "_"))
        return self.base_dir / f"{safe_name or 'recording'}.mp4"

    def status(self) -> dict:
        return {
            "enabled": True,
            "base_dir": str(self.base_dir),
        }


class WifiService:
    def get_current_wifi(self) -> dict:
        system = platform.system().lower()
        detectors = []
        if system == "darwin":
            detectors = [self._detect_macos_wifi]
        elif system == "linux":
            detectors = [self._detect_linux_wifi]

        for detector in detectors:
            info = detector()
            if info:
                return info

        return {
            "connected": False,
            "ssid": None,
            "interface": None,
            "source": system or "unknown",
            "error": "Wi-Fi non rilevato",
        }

    def _run_command(self, command: list[str]) -> str:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return ""
        return completed.stdout.strip()

    def _detect_macos_wifi(self) -> dict | None:
        hardware = self._run_command(["networksetup", "-listallhardwareports"])
        if not hardware:
            return None

        interface = None
        blocks = hardware.split("\n\n")
        for block in blocks:
            if "Hardware Port: Wi-Fi" not in block:
                continue
            match = re.search(r"Device: (\S+)", block)
            if match:
                interface = match.group(1)
                break

        if not interface:
            return None

        network = self._run_command(["networksetup", "-getairportnetwork", interface])
        if not network:
            return {
                "connected": False,
                "ssid": None,
                "interface": interface,
                "source": "networksetup",
                "error": "Stato Wi-Fi non disponibile",
            }

        if "You are not associated" in network:
            return {
                "connected": False,
                "ssid": None,
                "interface": interface,
                "source": "networksetup",
                "error": "Nessuna rete Wi-Fi collegata",
            }

        match = re.search(r"Current Wi-Fi Network: (.+)$", network)
        ssid = match.group(1).strip() if match else None
        return {
            "connected": bool(ssid),
            "ssid": ssid,
            "interface": interface,
            "source": "networksetup",
            "error": "" if ssid else "SSID non rilevato",
        }

    def _detect_linux_wifi(self) -> dict | None:
        ssid = self._run_command(["iwgetid", "-r"])
        if ssid:
            return {
                "connected": True,
                "ssid": ssid,
                "interface": None,
                "source": "iwgetid",
                "error": "",
            }
        return None


class CameraProfileService:
    def __init__(
        self,
        store_path: str = "data/camera_profiles.json",
        key_path: str | Path | None = None,
    ):
        self.store_path = Path(store_path)
        default_key_path = self.store_path.with_name(f".{self.store_path.stem}.key")
        self.secret_cipher = ProfileSecretCipher(key_path or default_key_path)

    def list_profiles(self) -> list[dict]:
        data = self._read_store()
        active_id = data.get("active_profile_id")
        profiles = []
        for profile in data.get("profiles", []):
            rendered = self._sanitize_profile(profile)
            rendered["active"] = rendered["id"] == active_id
            profiles.append(rendered)
        return profiles

    def get_active_profile_id(self) -> str | None:
        return self._read_store().get("active_profile_id")

    def ensure_default_profile(self, profile: dict) -> None:
        data = self._read_store()
        profiles = data.get("profiles", [])
        existing = next((item for item in profiles if item.get("id") == profile["id"]), None)
        if existing is None:
            profiles.append(self._normalize_profile(profile))
        else:
            existing.update(self._normalize_profile(profile))
        data["profiles"] = profiles
        data["active_profile_id"] = data.get("active_profile_id") or profile["id"]
        self._write_store(data)

    def save_profile(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Payload camera non valido")

        profile_id = str(payload.get("id") or uuid.uuid4())
        profile = self._normalize_profile({"id": profile_id, **payload})

        data = self._read_store()
        existing = next(
            (item for item in data.get("profiles", []) if item.get("id") == profile_id),
            None,
        )
        # On edit, blank secret fields keep the previously stored values so the user
        # does not have to re-type passwords just to change other settings.
        if existing:
            if not profile["password"]:
                profile["password"] = existing.get("password", "")
            if not profile["onvif_password"]:
                profile["onvif_password"] = existing.get("onvif_password", "")

        if not profile["name"]:
            raise ValueError("Nome camera obbligatorio")
        if not profile["host"]:
            raise ValueError("Host camera obbligatorio")
        if not profile["username"]:
            raise ValueError("Username RTSP obbligatorio")
        if not profile["password"]:
            raise ValueError("Password RTSP obbligatoria")

        profiles = [item for item in data.get("profiles", []) if item.get("id") != profile_id]
        profiles.append(profile)
        data["profiles"] = profiles
        if payload.get("activate") or not data.get("active_profile_id"):
            data["active_profile_id"] = profile_id
        self._write_store(data)
        rendered = self._sanitize_profile(profile)
        rendered["active"] = data.get("active_profile_id") == profile_id
        return rendered

    def activate_profile(self, profile_id: str) -> dict:
        data = self._read_store()
        profile = next(
            (item for item in data.get("profiles", []) if item.get("id") == profile_id), None
        )
        if not profile:
            raise ValueError("Profilo camera non trovato")
        data["active_profile_id"] = profile_id
        self._write_store(data)
        rendered = self._sanitize_profile(profile)
        rendered["active"] = True
        return rendered

    def delete_profile(self, profile_id: str) -> str | None:
        """Remove a profile. If it was active, fall back to another profile (or none).

        Returns the new active profile id (which may be None).
        """
        data = self._read_store()
        profiles = data.get("profiles", [])
        if not any(item.get("id") == profile_id for item in profiles):
            raise ValueError("Profilo camera non trovato")
        remaining = [item for item in profiles if item.get("id") != profile_id]
        data["profiles"] = remaining
        if data.get("active_profile_id") == profile_id:
            data["active_profile_id"] = remaining[0]["id"] if remaining else None
        self._write_store(data)
        return data.get("active_profile_id")

    def get_profile(self, profile_id: str) -> dict | None:
        data = self._read_store()
        for profile in data.get("profiles", []):
            if profile.get("id") == profile_id:
                return dict(profile)
        return None

    def build_runtime_updates(self, profile: dict) -> dict:
        if not profile:
            raise ValueError("Profilo camera mancante")

        onvif_username = profile.get("onvif_username") or profile.get("username")
        onvif_password = profile.get("onvif_password") or profile.get("password")
        return {
            "TAPO_HOST": profile["host"],
            "TAPO_RTSP_PORT": int(profile["rtsp_port"]),
            "TAPO_STREAM_PATH": profile["stream_path"],
            "TAPO_USERNAME": profile["username"],
            "TAPO_PASSWORD": profile["password"],
            "TAPO_ONVIF_PORT": int(profile["onvif_port"]),
            "TAPO_ONVIF_USERNAME": onvif_username,
            "TAPO_ONVIF_PASSWORD": onvif_password,
            "TAPO_MOVE_SPEED": float(profile["move_speed"]),
            "TAPO_MOVE_TIMEOUT": float(profile["move_timeout"]),
            "MOTION_SAVE_DIR": self._build_motion_dir(profile["id"]),
        }

    def _read_store(self) -> dict:
        if not self.store_path.exists():
            return {"active_profile_id": None, "profiles": []}
        try:
            data = json.loads(self.store_path.read_text())
        except json.JSONDecodeError:
            return {"active_profile_id": None, "profiles": []}
        if not isinstance(data, dict):
            return {"active_profile_id": None, "profiles": []}
        data.setdefault("active_profile_id", None)
        profiles = []
        should_migrate = False
        try:
            for raw_profile in data.get("profiles", []):
                if not isinstance(raw_profile, dict):
                    continue
                profile = dict(raw_profile)
                for field in CAMERA_PROFILE_SECRET_FIELDS:
                    decrypted, was_plaintext = self.secret_cipher.decrypt(profile.get(field, ""))
                    profile[field] = decrypted
                    should_migrate = should_migrate or was_plaintext
                profiles.append(profile)
        except ValueError:
            backup_path = self._backup_unreadable_store()
            logger.error(
                "Archivio profili camera non decifrabile. "
                "Backup creato in %s e store reinizializzato.",
                backup_path,
            )
            return {"active_profile_id": None, "profiles": []}
        data["profiles"] = profiles
        if should_migrate:
            self._write_store(data)
        return data

    def _backup_unreadable_store(self) -> Path:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = self.store_path.with_suffix(
            f"{self.store_path.suffix}.unreadable.{timestamp}.bak"
        )
        backup_path.write_bytes(self.store_path.read_bytes())
        _set_private_permissions(backup_path)
        self.store_path.unlink(missing_ok=True)
        return backup_path

    def _write_store(self, data: dict) -> None:
        persisted = dict(data)
        persisted_profiles = []
        for raw_profile in data.get("profiles", []):
            if not isinstance(raw_profile, dict):
                continue
            profile = dict(raw_profile)
            for field in CAMERA_PROFILE_SECRET_FIELDS:
                profile[field] = self.secret_cipher.encrypt(str(profile.get(field, "") or ""))
            persisted_profiles.append(profile)
        persisted["profiles"] = persisted_profiles
        _write_private_text(self.store_path, json.dumps(persisted, indent=2) + "\n")

    def _normalize_profile(self, payload: dict) -> dict:
        def read_text(key: str, default: str = "") -> str:
            return str(payload.get(key, default)).strip()

        def read_int(key: str, default: int) -> int:
            return int(payload.get(key) or default)

        def read_float(key: str, default: float) -> float:
            return float(payload.get(key) or default)

        def read_bool(key: str, default: bool = False) -> bool:
            value = payload.get(key, default)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        return {
            "id": read_text("id"),
            "name": read_text("name"),
            "wifi_ssid": read_text("wifi_ssid"),
            "host": read_text("host"),
            "rtsp_port": read_int("rtsp_port", 554),
            "stream_path": read_text("stream_path", "stream1") or "stream1",
            "username": read_text("username"),
            "password": read_text("password"),
            "onvif_port": read_int("onvif_port", 2020),
            "onvif_username": read_text("onvif_username"),
            "onvif_password": read_text("onvif_password"),
            "move_speed": read_float("move_speed", 0.6),
            "move_timeout": read_float("move_timeout", 0.35),
            "monitored": read_bool("monitored", False),
            "notes": read_text("notes"),
        }

    def _sanitize_profile(self, profile: dict) -> dict:
        rendered = dict(profile)
        rendered["viewer_url"] = f"/camera/{rendered['id']}"
        rendered["motion_save_dir"] = self._build_motion_dir(rendered["id"])
        rendered["password_set"] = bool(rendered.get("password"))
        rendered["onvif_password_set"] = bool(
            rendered.get("onvif_password") or rendered.get("password")
        )
        rendered.pop("password", None)
        rendered.pop("onvif_password", None)
        return rendered

    def _build_motion_dir(self, profile_id: str) -> str:
        safe_id = "".join(char for char in str(profile_id) if char.isalnum() or char in ("-", "_"))
        return str(Path("captures/motion") / (safe_id or "default"))


@dataclass
class FeatureServices:
    presets: PresetService
    notifications: NotificationService
    recording: RecordingService
    camera_profiles: CameraProfileService
    wifi: WifiService
    telegram: object | None = None
