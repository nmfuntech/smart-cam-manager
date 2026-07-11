import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class ConfigField:
    key: str
    value_type: str
    sensitive: bool = False
    internal_only: bool = False
    minimum: float | None = None
    must_be_odd: bool = False
    # Modificabile dalla UI web via PATCH /api/runtime_config. Fonte unica
    # dell'allowlist (prima duplicata in routes/motion.py): mai derivarla da
    # "not sensitive" — esporrebbe TAPO_HOST e simili, che appartengono ai
    # profili camera, non alla pagina impostazioni.
    ui_editable: bool = False


# Chiavi esposte alla pagina Impostazioni / sidebar viewer. Applicate ai
# ConfigField in __init__ (vedi ConfigField.ui_editable).
_UI_EDITABLE_KEYS = frozenset(
    {
        "MOTION_ENABLED",
        "MOTION_THRESHOLD",
        "MOTION_MIN_AREA",
        "MOTION_BLUR_SIZE",
        "MOTION_MOG2_HISTORY",
        "MOTION_GLOBAL_CHANGE_RATIO",
        "MOTION_MORPH_KERNEL",
        "CLASSIFICATION_ENABLED",
        "CLASSIFICATION_BACKEND",
        "CLASSIFICATION_MIN_CONFIDENCE",
        "CLASSIFICATION_SAMPLE_POLICY",
        "CLASSIFICATION_DETECT_PERSON",
        "CLASSIFICATION_DETECT_PET",
        "MOTION_RETENTION_DAYS",
        "MOTION_RETENTION_MAX_MB",
        "RECORD_ENABLED",
        "RECORD_FPS",
        "RECORD_PREROLL_SEC",
        "RECORD_MAX_DURATION_SEC",
        "RECORD_MAX_WIDTH",
        "NOTIFY_TELEGRAM_ENABLED",
        "NOTIFY_MIN_INTERVAL_SEC",
        "NOTIFY_PREFER_VIDEO",
        "CONTINUOUS_RECORD_ENABLED",
        "CONTINUOUS_RECORD_SEGMENT_MIN",
        "CONTINUOUS_RECORD_RETAIN_HOURS",
    }
)


class RuntimeConfigManager:
    """Manage editable env config with validation and .env persistence."""

    def __init__(self, env_path: str | Path = ".env"):
        self.env_path = Path(env_path)
        self.fields = {
            "TAPO_HOST": ConfigField("TAPO_HOST", "str"),
            "TAPO_RTSP_PORT": ConfigField("TAPO_RTSP_PORT", "int", minimum=1),
            "TAPO_STREAM_PATH": ConfigField("TAPO_STREAM_PATH", "str"),
            "TAPO_USERNAME": ConfigField("TAPO_USERNAME", "str", sensitive=True),
            "TAPO_PASSWORD": ConfigField("TAPO_PASSWORD", "str", sensitive=True),
            "TAPO_ONVIF_PORT": ConfigField("TAPO_ONVIF_PORT", "int", minimum=1),
            "TAPO_ONVIF_USERNAME": ConfigField("TAPO_ONVIF_USERNAME", "str", sensitive=True),
            "TAPO_ONVIF_PASSWORD": ConfigField("TAPO_ONVIF_PASSWORD", "str", sensitive=True),
            "TAPO_MOVE_SPEED": ConfigField("TAPO_MOVE_SPEED", "float", minimum=0),
            "TAPO_MOVE_TIMEOUT": ConfigField("TAPO_MOVE_TIMEOUT", "float", minimum=0),
            "MOTION_ENABLED": ConfigField("MOTION_ENABLED", "bool"),
            "MOTION_SAVE_DIR": ConfigField(
                "MOTION_SAVE_DIR",
                "str",
                internal_only=True,
            ),
            "MOTION_MIN_AREA": ConfigField("MOTION_MIN_AREA", "int", minimum=1),
            "MOTION_THRESHOLD": ConfigField("MOTION_THRESHOLD", "int", minimum=1),
            "MOTION_FRAME_INTERVAL": ConfigField(
                "MOTION_FRAME_INTERVAL", "float", minimum=0.01, internal_only=True
            ),
            "MOTION_CAPTURE_INTERVAL": ConfigField(
                "MOTION_CAPTURE_INTERVAL", "float", minimum=0.01, internal_only=True
            ),
            "MOTION_WARMUP_FRAMES": ConfigField(
                "MOTION_WARMUP_FRAMES", "int", minimum=0, internal_only=True
            ),
            "MOTION_TRIGGER_FRAMES": ConfigField(
                "MOTION_TRIGGER_FRAMES", "int", minimum=1, internal_only=True
            ),
            "MOTION_CLEAR_FRAMES": ConfigField(
                "MOTION_CLEAR_FRAMES", "int", minimum=1, internal_only=True
            ),
            "MOTION_EVENT_GAP": ConfigField(
                "MOTION_EVENT_GAP", "float", minimum=0, internal_only=True
            ),
            "MOTION_EVENT_MAX_DURATION": ConfigField(
                "MOTION_EVENT_MAX_DURATION", "float", minimum=1, internal_only=True
            ),
            "MOTION_BLUR_SIZE": ConfigField(
                "MOTION_BLUR_SIZE",
                "int",
                minimum=1,
                must_be_odd=True,
            ),
            "MOTION_MOG2_HISTORY": ConfigField("MOTION_MOG2_HISTORY", "int", minimum=1),
            "MOTION_SCALE_WIDTH": ConfigField("MOTION_SCALE_WIDTH", "int", minimum=0),
            "MOTION_MORPH_KERNEL": ConfigField(
                "MOTION_MORPH_KERNEL",
                "int",
                minimum=1,
                must_be_odd=True,
            ),
            "MOTION_MORPH_DILATE_ITER": ConfigField("MOTION_MORPH_DILATE_ITER", "int", minimum=0),
            "MOTION_GLOBAL_CHANGE_RATIO": ConfigField(
                "MOTION_GLOBAL_CHANGE_RATIO", "float", minimum=0
            ),
            "MOTION_LEARNING_RATE": ConfigField("MOTION_LEARNING_RATE", "float"),
            "MOTION_LEARNING_RATE_ACTIVE": ConfigField(
                "MOTION_LEARNING_RATE_ACTIVE", "float", minimum=0
            ),
            "CLASSIFICATION_ENABLED": ConfigField("CLASSIFICATION_ENABLED", "bool"),
            "CLASSIFICATION_BACKEND": ConfigField("CLASSIFICATION_BACKEND", "str"),
            "CLASSIFICATION_MIN_CONFIDENCE": ConfigField(
                "CLASSIFICATION_MIN_CONFIDENCE",
                "float",
                minimum=0.0,
            ),
            "CLASSIFICATION_SAMPLE_POLICY": ConfigField(
                "CLASSIFICATION_SAMPLE_POLICY",
                "str",
            ),
            "CLASSIFICATION_LOCAL_MODEL_PATH": ConfigField(
                "CLASSIFICATION_LOCAL_MODEL_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_LOCAL_LABELS_PATH": ConfigField(
                "CLASSIFICATION_LOCAL_LABELS_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_TM_MODEL_PATH": ConfigField(
                "CLASSIFICATION_TM_MODEL_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_TM_LABELS_PATH": ConfigField(
                "CLASSIFICATION_TM_LABELS_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_DETECTION_MODEL_PATH": ConfigField(
                "CLASSIFICATION_DETECTION_MODEL_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_DETECTION_CONFIG_PATH": ConfigField(
                "CLASSIFICATION_DETECTION_CONFIG_PATH",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_DETECTION_INPUT_SIZE": ConfigField(
                "CLASSIFICATION_DETECTION_INPUT_SIZE",
                "int",
                minimum=64,
            ),
            "CLASSIFICATION_DETECT_PERSON": ConfigField("CLASSIFICATION_DETECT_PERSON", "bool"),
            "CLASSIFICATION_DETECT_PET": ConfigField("CLASSIFICATION_DETECT_PET", "bool"),
            "CLASSIFICATION_CLIP_MAX_FRAMES": ConfigField(
                "CLASSIFICATION_CLIP_MAX_FRAMES", "int", minimum=1, internal_only=True
            ),
            "CLASSIFICATION_EARLY_EXIT_CONF": ConfigField(
                "CLASSIFICATION_EARLY_EXIT_CONF", "float", minimum=0, internal_only=True
            ),
            "CLASSIFICATION_CROP_TO_MOTION": ConfigField("CLASSIFICATION_CROP_TO_MOTION", "bool"),
            "CLASSIFICATION_PET_PRIORITY_MARGIN": ConfigField(
                "CLASSIFICATION_PET_PRIORITY_MARGIN", "float", minimum=0, internal_only=True
            ),
            "CLASSIFICATION_CROP_PADDING": ConfigField(
                "CLASSIFICATION_CROP_PADDING", "float", minimum=0.0
            ),
            "CLASSIFICATION_CLOUD_ENDPOINT": ConfigField(
                "CLASSIFICATION_CLOUD_ENDPOINT",
                "str",
                internal_only=True,
            ),
            "CLASSIFICATION_CLOUD_API_KEY": ConfigField(
                "CLASSIFICATION_CLOUD_API_KEY",
                "str",
                sensitive=True,
            ),
            "MOTION_RETENTION_DAYS": ConfigField("MOTION_RETENTION_DAYS", "float", minimum=0),
            "MOTION_RETENTION_MAX_MB": ConfigField("MOTION_RETENTION_MAX_MB", "float", minimum=0),
            "MOTION_RETENTION_INTERVAL_SEC": ConfigField(
                "MOTION_RETENTION_INTERVAL_SEC",
                "float",
                minimum=60,
                internal_only=True,
            ),
            "RECORD_ENABLED": ConfigField("RECORD_ENABLED", "bool"),
            "RECORD_FPS": ConfigField("RECORD_FPS", "float", minimum=1),
            "RECORD_PREROLL_SEC": ConfigField("RECORD_PREROLL_SEC", "float", minimum=0),
            "RECORD_POSTROLL_SEC": ConfigField(
                "RECORD_POSTROLL_SEC", "float", minimum=0, internal_only=True
            ),
            "RECORD_MAX_DURATION_SEC": ConfigField("RECORD_MAX_DURATION_SEC", "float", minimum=1),
            "RECORD_MAX_WIDTH": ConfigField("RECORD_MAX_WIDTH", "int", minimum=0),
            "NOTIFY_TELEGRAM_ENABLED": ConfigField("NOTIFY_TELEGRAM_ENABLED", "bool"),
            "NOTIFY_TELEGRAM_BOT_TOKEN": ConfigField(
                "NOTIFY_TELEGRAM_BOT_TOKEN", "str", sensitive=True
            ),
            "NOTIFY_TELEGRAM_CHAT_ID": ConfigField(
                "NOTIFY_TELEGRAM_CHAT_ID", "str", sensitive=True
            ),
            "NOTIFY_MIN_INTERVAL_SEC": ConfigField("NOTIFY_MIN_INTERVAL_SEC", "float", minimum=0),
            "NOTIFY_PREFER_VIDEO": ConfigField("NOTIFY_PREFER_VIDEO", "bool"),
            "NOTIFY_QUEUE_MAX": ConfigField(
                "NOTIFY_QUEUE_MAX", "int", minimum=1, internal_only=True
            ),
            "NOTIFY_TELEGRAM_MAX_VIDEO_MB": ConfigField(
                "NOTIFY_TELEGRAM_MAX_VIDEO_MB", "float", minimum=1, internal_only=True
            ),
            "TELEGRAM_COMMANDS_ENABLED": ConfigField("TELEGRAM_COMMANDS_ENABLED", "bool"),
            "TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS": ConfigField(
                "TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS", "str", sensitive=True
            ),
            "TELEGRAM_COMMANDS_RATE_LIMIT_PER_MIN": ConfigField(
                "TELEGRAM_COMMANDS_RATE_LIMIT_PER_MIN", "int", minimum=1
            ),
            "TELEGRAM_COMMANDS_POLL_TIMEOUT_SEC": ConfigField(
                "TELEGRAM_COMMANDS_POLL_TIMEOUT_SEC", "float", minimum=1
            ),
            "TELEGRAM_COMMANDS_SET_MENU": ConfigField("TELEGRAM_COMMANDS_SET_MENU", "bool"),
            "TELEGRAM_CLIP_MAX_CONCURRENT": ConfigField(
                "TELEGRAM_CLIP_MAX_CONCURRENT", "int", minimum=1, internal_only=True
            ),
            "TELEGRAM_INVITE_CODE": ConfigField("TELEGRAM_INVITE_CODE", "str", sensitive=True),
            "CONTINUOUS_RECORD_ENABLED": ConfigField("CONTINUOUS_RECORD_ENABLED", "bool"),
            "CONTINUOUS_RECORD_SEGMENT_MIN": ConfigField(
                "CONTINUOUS_RECORD_SEGMENT_MIN", "float", minimum=1
            ),
            "CONTINUOUS_RECORD_RETAIN_HOURS": ConfigField(
                "CONTINUOUS_RECORD_RETAIN_HOURS", "float", minimum=0.1
            ),
            "CONTINUOUS_RECORD_DIR": ConfigField(
                "CONTINUOUS_RECORD_DIR", "str", internal_only=True
            ),
            "AUTOMATION_ENABLED": ConfigField("AUTOMATION_ENABLED", "bool"),
            "AUTOMATION_QUEUE_MAX": ConfigField(
                "AUTOMATION_QUEUE_MAX", "int", minimum=1, internal_only=True
            ),
            "AUTOMATION_RULES_PATH": ConfigField(
                "AUTOMATION_RULES_PATH", "str", internal_only=True
            ),
            "AUTOMATION_DEVICES_PATH": ConfigField(
                "AUTOMATION_DEVICES_PATH", "str", internal_only=True
            ),
            "AGENT_ENABLED": ConfigField("AGENT_ENABLED", "bool"),
            "AGENT_OLLAMA_NUM_CTX": ConfigField(
                "AGENT_OLLAMA_NUM_CTX", "int", minimum=256, internal_only=True
            ),
            "AGENT_OLLAMA_NUM_PREDICT": ConfigField(
                "AGENT_OLLAMA_NUM_PREDICT", "int", minimum=8, internal_only=True
            ),
            "AGENT_OLLAMA_KEEP_ALIVE": ConfigField(
                "AGENT_OLLAMA_KEEP_ALIVE", "str", internal_only=True
            ),
            "AGENT_NATURAL_ANSWERS": ConfigField(
                "AGENT_NATURAL_ANSWERS", "bool", internal_only=True
            ),
            "AGENT_WARMUP": ConfigField("AGENT_WARMUP", "bool", internal_only=True),
            "AGENT_DOMAIN_GATE": ConfigField(
                "AGENT_DOMAIN_GATE", "bool", internal_only=True
            ),
            "AGENT_CACHE": ConfigField("AGENT_CACHE", "bool", internal_only=True),
            "STREAM_MAX_WIDTH": ConfigField(
                "STREAM_MAX_WIDTH", "int", minimum=0, internal_only=True
            ),
            "STREAM_JPEG_QUALITY": ConfigField(
                "STREAM_JPEG_QUALITY", "int", minimum=40, internal_only=True
            ),
            "STREAM_ENCODE_INTERVAL_MS": ConfigField(
                "STREAM_ENCODE_INTERVAL_MS", "int", minimum=0, internal_only=True
            ),
            "APP_MAX_MJPEG_STREAMS": ConfigField(
                "APP_MAX_MJPEG_STREAMS", "int", minimum=1, internal_only=True
            ),
            "APP_GUNICORN_THREADS": ConfigField(
                "APP_GUNICORN_THREADS", "int", minimum=1, internal_only=True
            ),
            "APP_WAITRESS_THREADS": ConfigField(
                "APP_WAITRESS_THREADS", "int", minimum=1, internal_only=True
            ),
            "OPENCV_NUM_THREADS": ConfigField(
                "OPENCV_NUM_THREADS", "int", minimum=1, internal_only=True
            ),
            "RTSP_BACKLOG_SKIP_FRAMES": ConfigField(
                "RTSP_BACKLOG_SKIP_FRAMES", "int", minimum=0, internal_only=True
            ),
            "APP_ENABLE_OPEN_FOLDER": ConfigField(
                "APP_ENABLE_OPEN_FOLDER", "bool", internal_only=True
            ),
        }
        for key in _UI_EDITABLE_KEYS:
            self.fields[key] = replace(self.fields[key], ui_editable=True)

    def public_update_keys(self) -> frozenset[str]:
        """Chiavi accettate da PATCH /api/runtime_config (allowlist UI)."""
        return frozenset(
            key
            for key, field in self.fields.items()
            if field.ui_editable and not field.sensitive and not field.internal_only
        )

    def get_public_config(self) -> dict:
        data = {}
        for key, field in self.fields.items():
            if field.sensitive or field.internal_only:
                continue
            raw_value = os.getenv(key)
            if raw_value is None:
                continue
            data[key] = self._coerce_value(key, raw_value, field)
        return data

    def get_values(self, keys) -> dict:
        data = {}
        for key in keys:
            field = self.fields.get(key)
            if field is None or field.sensitive:
                continue
            raw_value = os.getenv(key)
            data[key] = (
                self._coerce_value(key, raw_value, field) if raw_value is not None else None
            )
        return data

    def normalize_updates(
        self,
        updates: dict,
        allow_sensitive: bool = False,
        allow_internal: bool = False,
    ) -> dict:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("Payload aggiornamento non valido")
        normalized: dict[str, object] = {}
        for key, raw_value in updates.items():
            if key not in self.fields:
                raise ValueError(f"Parametro non modificabile: {key}")
            field = self.fields[key]
            if field.sensitive and not allow_sensitive:
                raise ValueError(f"Parametro sensibile non modificabile: {key}")
            if field.internal_only and not allow_internal:
                raise ValueError(f"Parametro interno non modificabile: {key}")
            normalized[key] = self._coerce_value(key, raw_value, field)
        return normalized

    def update(
        self,
        updates: dict,
        allow_sensitive: bool = False,
        allow_internal: bool = False,
    ) -> dict:
        normalized = self.normalize_updates(updates, allow_sensitive, allow_internal)

        for key, value in normalized.items():
            os.environ[key] = self._to_env_string(value)
        self._write_env(normalized)
        return self.get_public_config()

    def _coerce_value(self, key: str, raw_value, field: ConfigField):
        if field.value_type == "bool":
            value = self._parse_bool(raw_value)
        elif field.value_type == "int":
            value = int(raw_value)
        elif field.value_type == "float":
            value = float(raw_value)
        elif field.value_type == "str":
            value = str(raw_value).strip()
            if not value:
                raise ValueError(f"{key} non puo essere vuoto")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError(f"{key} contiene caratteri non validi")
        else:
            raise ValueError(f"Tipo non supportato: {field.value_type}")

        if isinstance(value, (int, float)) and field.minimum is not None and value < field.minimum:
            raise ValueError(f"{key} deve essere >= {field.minimum}")

        if key == "MOTION_THRESHOLD" and value > 255:
            raise ValueError("MOTION_THRESHOLD deve essere <= 255")

        if key == "CLASSIFICATION_MIN_CONFIDENCE" and value > 1:
            raise ValueError("CLASSIFICATION_MIN_CONFIDENCE deve essere <= 1")

        if key == "CLASSIFICATION_EARLY_EXIT_CONF" and value > 1:
            raise ValueError("CLASSIFICATION_EARLY_EXIT_CONF deve essere <= 1")

        if key == "STREAM_JPEG_QUALITY" and value > 100:
            raise ValueError("STREAM_JPEG_QUALITY deve essere <= 100")

        if key == "CLASSIFICATION_BACKEND" and value not in {
            "detection",
            "local",
            "teachable_machine",
            "cloud",
        }:
            raise ValueError(
                "CLASSIFICATION_BACKEND deve essere uno tra: "
                "detection, local, teachable_machine, cloud"
            )

        if field.must_be_odd and isinstance(value, int) and value % 2 == 0:
            value += 1

        return value

    def _parse_bool(self, raw_value) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Booleano non valido: {raw_value}")

    def _to_env_string(self, value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _write_env(self, updates: dict[str, object]) -> None:
        lines = []
        if self.env_path.exists():
            lines = self.env_path.read_text(encoding="utf-8").splitlines()

        pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
        pending = {key: self._to_env_string(value) for key, value in updates.items()}
        rendered: list[str] = []

        for line in lines:
            match = pattern.match(line)
            if not match:
                rendered.append(line)
                continue
            key = match.group(1)
            if key in pending:
                rendered.append(f"{key}={pending.pop(key)}")
            else:
                rendered.append(line)

        for key, value in pending.items():
            rendered.append(f"{key}={value}")

        content = "\n".join(rendered) + "\n"
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.env_path.parent,
            delete=False,
        ) as tmp:
            tmp.write(content)
            temp_path = Path(tmp.name)
        os.chmod(temp_path, 0o600)
        temp_path.replace(self.env_path)
        os.chmod(self.env_path, 0o600)
