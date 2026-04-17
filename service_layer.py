import json
from dataclasses import dataclass
from pathlib import Path


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


@dataclass
class FeatureServices:
    presets: PresetService
    notifications: NotificationService
    recording: RecordingService
