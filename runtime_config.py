import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConfigField:
    key: str
    value_type: str
    sensitive: bool = False
    minimum: float | None = None
    must_be_odd: bool = False


class RuntimeConfigManager:
    """Manage editable env config with validation and .env persistence."""

    def __init__(self, env_path: str | Path = ".env"):
        self.env_path = Path(env_path)
        self.fields = {
            "MOTION_ENABLED": ConfigField("MOTION_ENABLED", "bool"),
            "MOTION_MIN_AREA": ConfigField("MOTION_MIN_AREA", "int", minimum=1),
            "MOTION_THRESHOLD": ConfigField("MOTION_THRESHOLD", "int", minimum=1),
            "MOTION_BLUR_SIZE": ConfigField(
                "MOTION_BLUR_SIZE",
                "int",
                minimum=1,
                must_be_odd=True,
            ),
        }

    def get_public_config(self) -> dict:
        data = {}
        for key, field in self.fields.items():
            raw_value = os.getenv(key)
            if raw_value is None:
                continue
            data[key] = self._coerce_value(key, raw_value, field)
        return data

    def update(self, updates: dict) -> dict:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("Payload aggiornamento non valido")

        normalized: dict[str, object] = {}
        for key, raw_value in updates.items():
            if key not in self.fields:
                raise ValueError(f"Parametro non modificabile: {key}")
            field = self.fields[key]
            if field.sensitive:
                raise ValueError(f"Parametro sensibile non modificabile: {key}")
            normalized[key] = self._coerce_value(key, raw_value, field)

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
        else:
            raise ValueError(f"Tipo non supportato: {field.value_type}")

        if isinstance(value, (int, float)) and field.minimum is not None and value < field.minimum:
            raise ValueError(f"{key} deve essere >= {field.minimum}")

        if key == "MOTION_THRESHOLD" and value > 255:
            raise ValueError("MOTION_THRESHOLD deve essere <= 255")

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
        temp_path.replace(self.env_path)
