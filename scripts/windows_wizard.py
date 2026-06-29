"""Wizard interattivo configurazione BLACKFRAME su Windows mini PC."""

from __future__ import annotations

import argparse
import re
import sys
from getpass import getpass
from pathlib import Path

from scripts.env_profiles import (
    MINI_PC_WINDOWS,
    apply_profile,
    patch_env_file,
    write_windows_minipc_example,
)
from scripts.setup_config import (
    EXAMPLE_ENV_PATH,
    build_env_content,
    hash_admin_password,
    load_env_values,
    write_env_file,
)

WINDOWS_EXAMPLE_PATH = Path(".env.windows-minipc.example")


def _prompt(
    label: str,
    default: str = "",
    *,
    secret: bool = False,
    required: bool = False,
    help_text: str | None = None,
) -> str:
    hint = f" [{default}]" if default else ""
    extra = f" ({help_text})" if help_text else ""
    while True:
        if secret:
            raw = getpass(f"- {label}{hint}{extra}: ").strip()
        else:
            raw = input(f"- {label}{hint}{extra}: ").strip()
        if not raw:
            if default:
                return default
            if not required:
                return ""
            print("  Valore obbligatorio.")
            continue
        return raw


def _prompt_bool(label: str, default: bool) -> bool:
    default_text = "s" if default else "n"
    while True:
        raw = input(f"- {label} [s/n, default {default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"s", "si", "y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False
        print("  Rispondi s o n.")


def _prompt_choice(label: str, choices: dict[str, str], default_key: str) -> str:
    print(f"- {label}")
    for key, text in choices.items():
        marker = " (consigliato)" if key == default_key else ""
        print(f"    [{key}] {text}{marker}")
    while True:
        raw = input(f"  Scelta [{default_key}]: ").strip().lower()
        if not raw:
            return default_key
        if raw in choices:
            return raw
        print("  Scelta non valida.")


def _valid_host(value: str) -> bool:
    if not value.strip():
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", value.strip()):
        return True
    return bool(re.fullmatch(r"[a-zA-Z0-9.-]+", value.strip()))


def ensure_windows_example(root: Path) -> Path:
    example = root / WINDOWS_EXAMPLE_PATH
    base = root / EXAMPLE_ENV_PATH
    if not example.is_file():
        if not base.is_file():
            raise FileNotFoundError(f"File base mancante: {base}")
        write_windows_minipc_example(base_path=base, output_path=example)
    return example


def prepare_env_template(root: Path, env_path: Path, *, force: bool = False) -> None:
    if env_path.exists() and not force:
        return
    example = ensure_windows_example(root)
    env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")


def collect_wizard_values(existing: dict[str, str]) -> dict[str, str]:
    print("")
    print("== Configurazione BLACKFRAME (mini PC Windows) ==")
    print("Premi Invio per accettare il valore suggerito tra parentesi quadre.")
    print("")

    values = dict(existing)
    lan_access = _prompt_bool(
        "Accesso dall'interfaccia web da telefono/altri PC in LAN?",
        default=False,
    )
    values["APP_BIND_HOST"] = "0.0.0.0" if lan_access else "127.0.0.1"
    secure = values["APP_BIND_HOST"] != "127.0.0.1"
    values["APP_SESSION_COOKIE_SECURE"] = "true" if secure else "false"

    port_default = values.get("APP_PORT", "8000")
    port = _prompt("Porta web dell'app", port_default)
    values["APP_PORT"] = str(int(port))

    print("")
    print("== Credenziali admin web ==")
    admin_user = _prompt("Username admin", values.get("APP_ADMIN_USERNAME", "admin"))
    values["APP_ADMIN_USERNAME"] = admin_user
    admin_pw = _prompt(
        "Password admin (invio = genera automaticamente)",
        secret=True,
    )
    if not admin_pw:
        from scripts.setup_config import generate_admin_password

        admin_pw = generate_admin_password()
        print(f"  Generata password admin (salvala): {admin_pw}")
    values["APP_ADMIN_PASSWORD"] = admin_pw

    print("")
    print("== Telecamera Tapo ==")
    print("Trova IP e account RTSP nell'app Tapo")
    print("(Impostazioni avanzate → Gestione account camera).")
    cam_host = _prompt(
        "IP o hostname camera",
        values.get("TAPO_HOST", "192.168.1.50"),
        required=True,
    )
    while not _valid_host(cam_host):
        print("  Host non valido.")
        cam_host = _prompt("IP o hostname camera", required=True)
    values["TAPO_HOST"] = cam_host.strip()
    values["TAPO_USERNAME"] = _prompt(
        "Username RTSP camera",
        values.get("TAPO_USERNAME", ""),
        required=True,
    )
    values["TAPO_PASSWORD"] = _prompt(
        "Password RTSP camera",
        values.get("TAPO_PASSWORD", ""),
        secret=True,
        required=True,
    )
    values["TAPO_CAMERA_NAME"] = _prompt(
        "Nome visualizzato camera",
        values.get("TAPO_CAMERA_NAME", "Camera principale"),
    )
    values["TAPO_STREAM_PATH"] = MINI_PC_WINDOWS["TAPO_STREAM_PATH"]

    print("")
    print("== Telegram (opzionale, configurabile anche dall'interfaccia web) ==")
    if _prompt_bool("Configurare notifiche Telegram ora?", default=False):
        values["NOTIFY_TELEGRAM_ENABLED"] = "true"
        values["TELEGRAM_COMMANDS_ENABLED"] = "true"
        values["NOTIFY_TELEGRAM_BOT_TOKEN"] = _prompt(
            "Token bot Telegram",
            values.get("NOTIFY_TELEGRAM_BOT_TOKEN", ""),
            secret=True,
            required=True,
        )
        values["NOTIFY_TELEGRAM_CHAT_ID"] = _prompt(
            "Chat ID Telegram",
            values.get("NOTIFY_TELEGRAM_CHAT_ID", ""),
            required=True,
        )
    else:
        values["NOTIFY_TELEGRAM_ENABLED"] = values.get("NOTIFY_TELEGRAM_ENABLED", "false")
        values["TELEGRAM_COMMANDS_ENABLED"] = values.get("TELEGRAM_COMMANDS_ENABLED", "false")

    return values


def write_configured_env(root: Path, env_path: Path, values: dict[str, str]) -> None:
    ensure_windows_example(root)
    example_values = load_env_values(root / WINDOWS_EXAMPLE_PATH)
    example_values.update(MINI_PC_WINDOWS)
    example_values.update(values)
    hash_admin_password(example_values)
    content = build_env_content(example_values)
    write_env_file(env_path, content)
    apply_profile(env_path, "mini-pc-windows")


def prompt_service_mode() -> str:
    print("")
    print("== Servizio sempre attivo ==")
    return _prompt_choice(
        "Come vuoi avviare BLACKFRAME al boot?",
        {
            "nssm": "Servizio Windows con NSSM — consigliato (riavvio automatico su crash)",
            "task": "Utilità di pianificazione — più semplice, senza restart su crash",
            "manual": "Solo avvio manuale — nessun avvio automatico",
        },
        default_key="nssm",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wizard configurazione BLACKFRAME su Windows")
    parser.add_argument("--root", default=".", help="Cartella progetto")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--force", action="store_true", help="Rigenera .env anche se esiste")
    parser.add_argument("--non-interactive", action="store_true", help="Solo template + profilo")
    parser.add_argument("--service-mode", choices=("nssm", "task", "manual"), default="")
    parser.add_argument("--write-example-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    env_path = root / args.env_file

    if args.write_example_only:
        write_windows_minipc_example(
            base_path=root / EXAMPLE_ENV_PATH,
            output_path=root / WINDOWS_EXAMPLE_PATH,
        )
        print(f"Scritto {root / WINDOWS_EXAMPLE_PATH}")
        return 0

    try:
        prepare_env_template(root, env_path, force=args.force)
        existing = load_env_values(env_path) if env_path.exists() else load_env_values(
            root / WINDOWS_EXAMPLE_PATH
        )
        if args.non_interactive:
            patch_env_file(env_path, MINI_PC_WINDOWS)
            print(f"Profilo mini-pc-windows applicato a {env_path}")
            return 0

        values = collect_wizard_values(existing)
        write_configured_env(root, env_path, values)

        service_mode = args.service_mode or prompt_service_mode()
        print("")
        print(f"Configurazione salvata in {env_path}")
        print(f"Modalità servizio scelta: {service_mode}")
        print(f"LAN: http://{values['APP_BIND_HOST']}:{values['APP_PORT']}")
        if values.get("APP_ADMIN_PASSWORD"):
            print("Password admin: quella inserita/generata sopra (salvala in un posto sicuro).")
        print("")
        print("__SERVICE_MODE__=" + service_mode)
        print("__APP_PORT__=" + values["APP_PORT"])
        print("__LAN_ACCESS__=" + ("true" if values["APP_BIND_HOST"] == "0.0.0.0" else "false"))
        return 0
    except KeyboardInterrupt:
        print("\nWizard annullato.")
        return 1
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
