#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import os
import re
import secrets
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Callable

from werkzeug.security import generate_password_hash

from scripts.env_profiles import active_platform_profile, format_env_value

DEFAULT_ENV_PATH = Path(".env")
EXAMPLE_ENV_PATH = Path(".env.example")
DEFAULT_CAPTURES_PATH = Path("captures/motion")
GENERATE_COMMANDS = {"g", "gen", "generate", "/g", "/generate"}
LOCAL_BIND_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Re-running setup wipes these. Losing the camera encryption key or the profiles
# store is irreversible: the camera passwords encrypted with that key can no
# longer be decrypted. Guard against running setup on a live installation.
PROFILE_KEY_PATH = Path("data/.camera_profiles.key")
PROFILE_STORE_PATH = Path("data/camera_profiles.json")
RESET_CONFIRM_WORD = "reset"


Parser = Callable[[str], str]
Generator = Callable[[], str]
DefaultResolver = Callable[[dict[str, str]], str | None]


@dataclass(frozen=True)
class EnvField:
    key: str
    prompt: str
    parser: Parser
    default: str | None = None
    allow_empty: bool = False
    secret: bool = False
    generator: Generator | None = None
    default_resolver: DefaultResolver | None = None
    help_text: str | None = None
    include_in_minimal: bool = False
    # Derived fields are never prompted: their value is computed from other fields
    # before the .env is written (e.g. the admin password hash).
    derived: bool = False


@dataclass(frozen=True)
class EnvSection:
    title: str
    description: str | None
    fields: tuple[EnvField, ...]


def parse_text(value: str) -> str:
    return value.strip()


def parse_int(value: str) -> str:
    return str(int(value.strip()))


def parse_float(value: str) -> str:
    numeric = float(value.strip())
    return format(numeric, "g")


def parse_bool(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "si", "s"}:
        return "true"
    if normalized in {"0", "false", "no", "n", "off"}:
        return "false"
    raise ValueError("Inserisci true/false, yes/no oppure 1/0")


def generate_admin_password() -> str:
    return secrets.token_urlsafe(24)


def generate_app_secret_key() -> str:
    return secrets.token_hex(32)


def generate_profile_encryption_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def _secure_cookie_default(values: dict[str, str]) -> str:
    bind_host = values.get("APP_BIND_HOST", "127.0.0.1").strip().lower()
    return "false" if bind_host in LOCAL_BIND_HOSTS else "true"


def _profile_encryption_key_default(values: dict[str, str]) -> str | None:
    current_value = values.get("APP_PROFILE_ENCRYPTION_KEY", "").strip()
    if current_value:
        return current_value

    key_path = Path("data/.camera_profiles.key")
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()

    return None


SECTIONS: tuple[EnvSection, ...] = (
    EnvSection(
        title="Applicazione e sicurezza",
        description="Accesso admin, bind rete, cookie e chiavi applicative.",
        fields=(
            EnvField("APP_ADMIN_USERNAME", "Username admin", parse_text, default="admin"),
            EnvField(
                "APP_ADMIN_PASSWORD",
                "Password admin",
                parse_text,
                secret=True,
                generator=generate_admin_password,
                help_text="Invio genera password forte. Salvata solo come hash.",
                include_in_minimal=True,
            ),
            # Only the hash is persisted; APP_ADMIN_PASSWORD is blanked before write.
            EnvField(
                "APP_ADMIN_PASSWORD_HASH",
                "Hash password admin",
                parse_text,
                allow_empty=True,
                derived=True,
            ),
            EnvField(
                "APP_SECRET_KEY",
                "APP_SECRET_KEY",
                parse_text,
                secret=True,
                generator=generate_app_secret_key,
                help_text="Chiave sessione Flask. Invio genera valore forte.",
                include_in_minimal=True,
            ),
            EnvField("APP_BIND_HOST", "Host bind app", parse_text, default="127.0.0.1"),
            EnvField("APP_PORT", "Porta app", parse_int, default="8000"),
            EnvField(
                "APP_SESSION_COOKIE_SECURE",
                "Cookie sicuri HTTPS",
                parse_bool,
                default_resolver=_secure_cookie_default,
                help_text="true dietro HTTPS, false in locale HTTP.",
            ),
            EnvField(
                "APP_PROFILE_ENCRYPTION_KEY",
                "Chiave cifratura profili camera",
                parse_text,
                secret=True,
                generator=generate_profile_encryption_key,
                default_resolver=_profile_encryption_key_default,
                help_text="Invio genera chiave Fernet forte.",
                include_in_minimal=True,
            ),
        ),
    ),
    EnvSection(
        title="Camera e accesso RTSP/ONVIF",
        description="Parametri principali camera, nome profilo e credenziali.",
        fields=(
            EnvField("TAPO_CAMERA_NAME", "Nome camera", parse_text, default="Camera principale"),
            EnvField(
                "TAPO_WIFI_SSID",
                "SSID Wi-Fi associato",
                parse_text,
                default="",
                allow_empty=True,
            ),
            EnvField(
                "TAPO_HOST",
                "IP o hostname camera",
                parse_text,
                default="192.168.1.50",
                include_in_minimal=True,
            ),
            EnvField("TAPO_RTSP_PORT", "Porta RTSP", parse_int, default="554"),
            EnvField(
                "TAPO_STREAM_PATH",
                "Stream path RTSP",
                parse_text,
                default="stream2" if sys.platform == "win32" else "stream1",
                help_text="stream2 = sottostream SD (consigliato su mini PC Windows).",
            ),
            EnvField("TAPO_USERNAME", "Username RTSP", parse_text, include_in_minimal=True),
            EnvField(
                "TAPO_PASSWORD",
                "Password RTSP",
                parse_text,
                secret=True,
                include_in_minimal=True,
            ),
            EnvField("TAPO_ONVIF_PORT", "Porta ONVIF", parse_int, default="2020"),
            EnvField(
                "TAPO_ONVIF_USERNAME",
                "Username ONVIF",
                parse_text,
                default="",
                allow_empty=True,
                help_text="Vuoto = usa fallback RTSP.",
            ),
            EnvField(
                "TAPO_ONVIF_PASSWORD",
                "Password ONVIF",
                parse_text,
                default="",
                allow_empty=True,
                secret=True,
                help_text="Vuoto = usa fallback RTSP.",
            ),
            EnvField(
                "TAPO_CAMERA_ACCOUNT_USER",
                "Username account camera legacy",
                parse_text,
                default="",
                allow_empty=True,
            ),
            EnvField(
                "TAPO_CAMERA_ACCOUNT_PASSWORD",
                "Password account camera legacy",
                parse_text,
                default="",
                allow_empty=True,
                secret=True,
            ),
            EnvField("TAPO_MOVE_SPEED", "Velocita PTZ", parse_float, default="0.6"),
            EnvField("TAPO_MOVE_TIMEOUT", "Durata movimento PTZ", parse_float, default="0.35"),
        ),
    ),
    EnvSection(
        title="Motion detection",
        description="Sensibilita, timing e archivio eventi.",
        fields=(
            EnvField("MOTION_ENABLED", "Motion enabled", parse_bool, default="true"),
            EnvField("MOTION_MIN_AREA", "Area minima motion", parse_int, default="1800"),
            EnvField("MOTION_THRESHOLD", "Soglia motion", parse_int, default="55"),
            EnvField("MOTION_BLUR_SIZE", "Blur size motion", parse_int, default="7"),
            EnvField("MOTION_COOLDOWN", "Cooldown motion", parse_float, default="3"),
            EnvField(
                "MOTION_FRAME_INTERVAL", "Intervallo frame motion", parse_float, default="0.33"
            ),
            EnvField(
                "MOTION_CAPTURE_INTERVAL", "Intervallo capture motion", parse_float, default="0.25"
            ),
            EnvField(
                "MOTION_MAX_AREA_RATIO", "Rapporto area massima motion", parse_float, default="0.45"
            ),
            EnvField("MOTION_WARMUP_FRAMES", "Warmup frames motion", parse_int, default="30"),
            EnvField("MOTION_TRIGGER_FRAMES", "Trigger frames motion", parse_int, default="4"),
            EnvField("MOTION_CLEAR_FRAMES", "Clear frames motion", parse_int, default="10"),
            EnvField(
                "MOTION_BACKGROUND_ALPHA",
                "Background alpha motion",
                parse_float,
                default="0.03",
            ),
            EnvField("MOTION_SAVE_FRAMES", "Salva frame motion", parse_bool, default="true"),
            EnvField(
                "MOTION_SAVE_DIR",
                "Directory salvataggio motion",
                parse_text,
                default="captures/motion",
            ),
            EnvField("MOTION_EVENT_GAP", "Gap tra eventi motion", parse_float, default="4.0"),
            EnvField(
                "MOTION_EVENT_MAX_DURATION",
                "Durata max evento (s)",
                parse_float,
                default="45.0",
            ),
            EnvField(
                "MOTION_SCALE_WIDTH",
                "Larghezza frame analisi MOG2",
                parse_int,
                default="360",
            ),
            EnvField("MOTION_MOG2_HISTORY", "Storia MOG2", parse_int, default="500"),
            EnvField("MOTION_MORPH_KERNEL", "Kernel morfologia", parse_int, default="3"),
            EnvField("MOTION_MORPH_DILATE_ITER", "Iterazioni dilatazione", parse_int, default="2"),
            EnvField(
                "MOTION_GLOBAL_CHANGE_RATIO",
                "Soglia cambio globale luce",
                parse_float,
                default="0.4",
            ),
            EnvField("MOTION_LEARNING_RATE", "Learning rate MOG2", parse_float, default="-1"),
            EnvField(
                "MOTION_LEARNING_RATE_ACTIVE",
                "Learning rate MOG2 attivo",
                parse_float,
                default="0.0005",
            ),
        ),
    ),
    EnvSection(
        title="Registrazione video eventi",
        description="Clip MP4 per evento (richiede ffmpeg in PATH per H.264 nel browser).",
        fields=(
            EnvField("RECORD_ENABLED", "Registrazione clip evento", parse_bool, default="true"),
            EnvField("RECORD_FPS", "FPS registrazione", parse_float, default="8"),
            EnvField(
                "RECORD_PREROLL_SEC",
                "Pre-roll registrazione (s)",
                parse_float,
                default="2.0",
            ),
            EnvField("RECORD_MAX_DURATION_SEC", "Durata max clip (s)", parse_float, default="30"),
            EnvField("RECORD_MAX_WIDTH", "Larghezza max clip (px)", parse_int, default="960"),
        ),
    ),
    EnvSection(
        title="Classificazione persona/pet",
        description="Backend detection (scarica modelli con make fetch-model).",
        fields=(
            EnvField(
                "CLASSIFICATION_ENABLED",
                "Classificazione abilitata",
                parse_bool,
                default="true",
            ),
            EnvField(
                "CLASSIFICATION_BACKEND",
                "Backend classificazione",
                parse_text,
                default="detection",
            ),
            EnvField(
                "CLASSIFICATION_MIN_CONFIDENCE",
                "Confidenza minima",
                parse_float,
                default="0.55",
            ),
            EnvField(
                "CLASSIFICATION_SAMPLE_POLICY",
                "Policy campionamento",
                parse_text,
                default="event_cover",
            ),
            EnvField(
                "CLASSIFICATION_DETECT_PERSON",
                "Rileva persona",
                parse_bool,
                default="true",
            ),
            EnvField(
                "CLASSIFICATION_DETECT_PET",
                "Rileva animali",
                parse_bool,
                default="true",
            ),
            EnvField(
                "CLASSIFICATION_DETECTION_MODEL_PATH",
                "Percorso modello detection",
                parse_text,
                default="models/ssd_mobilenet_v2_coco.pb",
            ),
            EnvField(
                "CLASSIFICATION_DETECTION_CONFIG_PATH",
                "Percorso config detection",
                parse_text,
                default="models/ssd_mobilenet_v2_coco.pbtxt",
            ),
            EnvField(
                "CLASSIFICATION_DETECTION_INPUT_SIZE",
                "Input size detection",
                parse_int,
                default="300",
            ),
            EnvField(
                "CLASSIFICATION_CROP_TO_MOTION",
                "Ritaglia su bbox movimento",
                parse_bool,
                default="true",
            ),
            EnvField(
                "CLASSIFICATION_CROP_PADDING",
                "Padding ritaglio",
                parse_float,
                default="0.2",
            ),
        ),
    ),
    EnvSection(
        title="Stream tuning avanzato",
        description="Timeout, buffering e JPEG live. Premi invio per default consigliati.",
        fields=(
            EnvField(
                "RTSP_OPEN_TIMEOUT_SEC", "Timeout apertura RTSP (s)", parse_float, default="8.0"
            ),
            EnvField(
                "RTSP_RECONNECT_BACKOFF_MAX_SEC",
                "Backoff max reconnessione RTSP (s)",
                parse_float,
                default="15.0",
            ),
            EnvField(
                "STREAM_SNAPSHOT_INTERVAL_ONLINE_MS",
                "Intervallo snapshot online (ms)",
                parse_int,
                default="700",
            ),
            EnvField(
                "STREAM_SNAPSHOT_INTERVAL_OFFLINE_MS",
                "Intervallo snapshot offline (ms)",
                parse_int,
                default="2500",
            ),
            EnvField(
                "RTSP_BACKLOG_SKIP_FRAMES", "Frame backlog da saltare", parse_int, default="2"
            ),
            EnvField("STREAM_JPEG_QUALITY", "Qualita JPEG live", parse_int, default="85"),
            EnvField(
                "STREAM_MAX_WIDTH",
                "Larghezza massima stream (0 = nessun limite)",
                parse_int,
                default="0",
            ),
        ),
    ),
)


def load_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    pattern = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(raw_line)
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = bytes(value, "utf-8").decode("unicode_escape")
        values[key] = value
    return values


def selected_sections(minimal: bool) -> tuple[EnvSection, ...]:
    if not minimal:
        return SECTIONS

    filtered_sections: list[EnvSection] = []
    for section in SECTIONS:
        minimal_fields = tuple(field for field in section.fields if field.include_in_minimal)
        if minimal_fields:
            filtered_sections.append(
                EnvSection(
                    title=section.title,
                    description=section.description,
                    fields=minimal_fields,
                )
            )
    return tuple(filtered_sections)


def resolve_default(field: EnvField, values: dict[str, str]) -> str | None:
    if field.default_resolver is not None:
        return field.default_resolver(values)
    return field.default


def prompt_non_secret(
    field: EnvField,
    resolved_default: str | None,
) -> str:
    while True:
        suffix = f" [{resolved_default}]" if resolved_default not in {None, ""} else ""
        help_suffix = f" ({field.help_text})" if field.help_text else ""
        raw_value = input(f"- {field.prompt}{suffix}{help_suffix}: ").strip()
        if not raw_value:
            if resolved_default is not None:
                print(selected_value_message(field.key, resolved_default))
                return resolved_default
            if field.allow_empty:
                print(selected_value_message(field.key, ""))
                return ""
            print("  Valore obbligatorio.")
            continue
        try:
            return field.parser(raw_value)
        except ValueError as exc:
            print(f"  Valore non valido: {exc}")


def prompt_secret(
    field: EnvField,
    resolved_default: str | None,
) -> tuple[str, bool]:
    has_existing_value = bool(resolved_default)
    generated = False
    while True:
        hints: list[str] = []
        if has_existing_value:
            hints.append("invio = mantieni esistente")
        elif field.generator is not None:
            hints.append("invio = genera random")
        if field.generator is not None:
            hints.append("/generate = nuovo random")
        if field.allow_empty:
            hints.append("vuoto consentito")
        help_suffix = f" ({field.help_text})" if field.help_text else ""
        hint_text = f" [{' | '.join(hints)}]" if hints else ""
        raw_value = getpass(f"- {field.prompt}{hint_text}{help_suffix}: ").strip()

        if not raw_value:
            if has_existing_value:
                print(selected_value_message(field.key, resolved_default or ""))
                return resolved_default or "", False
            if field.generator is not None:
                generated_value = field.generator()
                print(generated_value_message(field.key, generated_value))
                return generated_value, True
            if field.allow_empty:
                print(selected_value_message(field.key, ""))
                return "", False
            print("  Valore obbligatorio.")
            continue

        if field.generator is not None and raw_value.lower() in GENERATE_COMMANDS:
            generated_value = field.generator()
            print(generated_value_message(field.key, generated_value))
            return generated_value, True

        try:
            return field.parser(raw_value), generated
        except ValueError as exc:
            print(f"  Valore non valido: {exc}")


def generated_value_message(key: str, value: str) -> str:
    return f"  Generato {key}: {value}"


def selected_value_message(key: str, value: str) -> str:
    return f"  Usato {key}: {value}"


def cleanup_setup_state(env_path: Path) -> list[Path]:
    removed: list[Path] = []
    paths_to_remove = [
        env_path,
        Path("data/.camera_profiles.key"),
        Path("data/.test-camera-profiles.key"),
        Path("data/camera_profiles.json"),
    ]
    for path in paths_to_remove:
        if path.exists():
            path.unlink()
            removed.append(path)

    for backup_path in sorted(Path("data").glob("camera_profiles.json.unreadable.*.bak")):
        backup_path.unlink()
        removed.append(backup_path)

    captures_path = DEFAULT_CAPTURES_PATH
    if captures_path.exists():
        for child in sorted(captures_path.iterdir(), reverse=True):
            if child.is_dir():
                for nested in sorted(child.rglob("*"), reverse=True):
                    if nested.is_file() or nested.is_symlink():
                        nested.unlink()
                    elif nested.is_dir():
                        nested.rmdir()
                child.rmdir()
            else:
                child.unlink()
        removed.append(captures_path)

    return removed


def hash_admin_password(values: dict[str, str]) -> None:
    """Persist only a hash of the admin password, never the plaintext.

    The plaintext (typed or generated) is hashed into APP_ADMIN_PASSWORD_HASH and
    APP_ADMIN_PASSWORD is blanked, so the written .env holds no recoverable secret.
    auth.verify_admin_password prefers the hash when present.
    """
    plaintext = values.get("APP_ADMIN_PASSWORD", "").strip()
    if plaintext:
        values["APP_ADMIN_PASSWORD_HASH"] = generate_password_hash(plaintext)
        values["APP_ADMIN_PASSWORD"] = ""


def merged_env_values(values: dict[str, str]) -> dict[str, str]:
    """Unisce .env.example, profilo piattaforma e valori raccolti dallo setup."""
    merged = load_env_values(EXAMPLE_ENV_PATH)
    merged.update(active_platform_profile())
    for section in SECTIONS:
        for field in section.fields:
            if field.key in values:
                merged[field.key] = values[field.key]
    merged.update(values)
    return merged


def build_env_content(values: dict[str, str]) -> str:
    merged = merged_env_values(values)
    header = [
        "# File generato da make setup",
        "# Modifica con cautela. I file sensibili vengono salvati con permessi privati.",
        "",
    ]
    if not EXAMPLE_ENV_PATH.exists():
        lines = list(header)
        for section in SECTIONS:
            lines.append(f"# {section.title}")
            if section.description:
                lines.append(f"# {section.description}")
            for field in section.fields:
                lines.append(f"{field.key}={format_env_value(merged.get(field.key, ''))}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    pattern = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
    out = list(header)
    for raw_line in EXAMPLE_ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            out.append(raw_line)
            continue
        match = pattern.match(raw_line)
        if match and match.group(2) in merged:
            key = match.group(2)
            out.append(f"{key}={format_env_value(merged[key])}")
        else:
            out.append(raw_line)
    return "\n".join(out).rstrip() + "\n"


def write_env_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)


def collect_values(
    existing_values: dict[str, str],
    minimal: bool = False,
) -> tuple[dict[str, str], dict[str, str]]:
    values = dict(existing_values)
    generated: dict[str, str] = {}
    sections = selected_sections(minimal)

    print("")
    print("BLACKFRAME setup interattivo")
    print("Invio mantiene default o valore esistente. Ctrl+C interrompe.")
    if minimal:
        print("Modalita` minimale: chiedo solo dati strettamente necessari.")

    for section in sections:
        print("")
        print(f"== {section.title} ==")
        if section.description:
            print(section.description)
        for field in section.fields:
            if field.derived:
                continue
            current_default = values.get(field.key)
            if current_default is None:
                current_default = resolve_default(field, values)

            if field.secret:
                value, was_generated = prompt_secret(field, current_default)
            else:
                value = prompt_non_secret(field, current_default)
                was_generated = False

            values[field.key] = value
            if was_generated:
                generated[field.key] = value
                print(generated_value_message(field.key, value))

    return values, generated


def existing_protected_state(env_path: Path) -> list[Path]:
    """Protected files that re-running setup would irreversibly destroy, if present."""
    candidates = [env_path, PROFILE_KEY_PATH, PROFILE_STORE_PATH]
    return [path for path in candidates if path.exists()]


def confirm_destructive_reset(
    existing: list[Path],
    input_fn: Callable[[str], str] = input,
    force: bool = False,
) -> bool:
    """Return True only if it is safe to wipe existing state.

    No existing state or --force returns True immediately. Otherwise the user must
    type the confirmation word; anything else aborts so a live installation's
    encryption key and camera profiles are not destroyed by accident.
    """
    if force or not existing:
        return True
    print("")
    print("⚠️  Configurazione esistente rilevata:")
    for path in existing:
        print(f"   - {path}")
    print(
        "Proseguendo verranno CANCELLATI .env, chiave di cifratura e profili camera.\n"
        "Le password camera cifrate diventeranno IRRECUPERABILI.\n"
        "Se è un'installazione già attiva, interrompi ora (Ctrl+C)."
    )
    answer = (
        input_fn(f"Scrivi '{RESET_CONFIRM_WORD}' per confermare la cancellazione: ").strip().lower()
    )
    return answer == RESET_CONFIRM_WORD


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Setup interattivo configurazione BLACKFRAME")
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_PATH),
        help="Percorso file .env da creare o aggiornare",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Chiede solo i parametri strettamente necessari e lascia il resto ai default",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Salta la conferma anche con configurazione esistente (uso non interattivo)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = Path(args.env_file)
    if not confirm_destructive_reset(existing_protected_state(env_path), force=args.force):
        print("")
        print("Setup annullato. Nessun file modificato.")
        return 1
    removed_paths = cleanup_setup_state(env_path)
    example_values = load_env_values(EXAMPLE_ENV_PATH)
    existing_values = dict(example_values)

    try:
        values, generated = collect_values(existing_values, minimal=args.minimal)
    except KeyboardInterrupt:
        print("")
        print("Setup annullato. Nessun file scritto.")
        return 1

    hash_admin_password(values)
    content = build_env_content(values)
    write_env_file(env_path, content)

    print("")
    print(f"Configurazione scritta in {env_path}")
    print("Permessi file impostati a 600")
    if removed_paths:
        print("Stato precedente pulito:")
        for path in removed_paths:
            print(f"- {path}")
    if generated:
        print("Segreti generati automaticamente:")
        for key, value in generated.items():
            print(f"- {key}: {value}")
    print("")
    print("Prossimi passi:")
    print("- make install")
    print("- make fetch-model          # modelli classificazione persona/pet")
    if sys.platform == "win32":
        print("- installa ffmpeg (winget install Gyan.FFmpeg) e riapri il terminale")
    print("- poetry run python scripts/check_prerequisites.py")
    print("- make run")
    print("- apri http://127.0.0.1:8000/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
