"""Profili di configurazione .env per hardware e piattaforma.

I profili aggiornano solo chiavi di tuning (motion, stream, registrazione).
Non toccano credenziali o segreti.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Tuning consigliato per mini PC Windows: meno carico CPU, meno falsi positivi,
# clip più leggere. Richiede ffmpeg in PATH per la riproduzione nel browser.
MINI_PC_WINDOWS: dict[str, str] = {
    "TAPO_STREAM_PATH": "stream2",
    "MOTION_THRESHOLD": "42",
    "MOTION_MIN_AREA": "1200",
    "MOTION_BLUR_SIZE": "7",
    "MOTION_FRAME_INTERVAL": "0.2",
    "MOTION_CAPTURE_INTERVAL": "0.35",
    "MOTION_WARMUP_FRAMES": "20",
    "MOTION_TRIGGER_FRAMES": "2",
    "MOTION_CLEAR_FRAMES": "10",
    "MOTION_SCALE_WIDTH": "420",
    "MOTION_EVENT_GAP": "5.0",
    "MOTION_EVENT_MAX_DURATION": "30.0",
    "MOTION_GLOBAL_CHANGE_RATIO": "0.4",
    "MOTION_MOG2_HISTORY": "500",
    "MOTION_MORPH_KERNEL": "3",
    "MOTION_MORPH_DILATE_ITER": "2",
    "RTSP_BACKLOG_SKIP_FRAMES": "0",
    "RECORD_ENABLED": "true",
    "RECORD_FPS": "8",
    "RECORD_MAX_WIDTH": "854",
    "RECORD_MAX_DURATION_SEC": "22",
    "RECORD_PREROLL_SEC": "2.0",
    "RECORD_POSTROLL_SEC": "2.0",
    "CLASSIFICATION_ENABLED": "true",
    "CLASSIFICATION_BACKEND": "detection",
    "CLASSIFICATION_MIN_CONFIDENCE": "0.58",
    "CLASSIFICATION_CROP_TO_MOTION": "false",
    "CLASSIFICATION_PET_PRIORITY_MARGIN": "0.12",
    "NOTIFY_MIN_INTERVAL_SEC": "6",
    "NOTIFY_TELEGRAM_MAX_VIDEO_MB": "20",
    "NOTIFY_PREFER_VIDEO": "true",
    "TELEGRAM_COMMANDS_ENABLED": "true",
    "APP_ENABLE_OPEN_FOLDER": "true",
}

PROFILES: dict[str, dict[str, str]] = {
    "mini-pc-windows": MINI_PC_WINDOWS,
}


def active_platform_profile() -> dict[str, str]:
    if sys.platform == "win32":
        return dict(MINI_PC_WINDOWS)
    return {}


def format_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() for char in value) or any(char in value for char in '#"\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


_ENV_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def patch_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """Aggiorna o aggiunge chiavi nel .env. Ritorna l'elenco delle chiavi modificate."""
    if not updates:
        return []

    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE.match(line)
        if match and match.group(2) in updates:
            key = match.group(2)
            out.append(f"{match.group(1)}{key}={format_env_value(updates[key])}")
            seen.add(key)
        else:
            out.append(line)

    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={format_env_value(value)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    if path.exists():
        try:
            import os

            os.chmod(path, 0o600)
        except OSError:
            pass
    return sorted(updates.keys())


def apply_profile(path: Path, profile_name: str) -> list[str]:
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(f"Profilo sconosciuto: {profile_name}")
    return patch_env_file(path, profile)


def load_env_values_from_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(raw_line)
        if not match:
            continue
        key = match.group(2)
        value = match.group(3).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = bytes(value, "utf-8").decode("unicode_escape")
        values[key] = value
    return values


def build_example_content(base_path: Path, profile: dict[str, str]) -> str:
    """Applica un profilo di tuning a .env.example preservando commenti e ordine."""
    if not base_path.is_file():
        raise FileNotFoundError(base_path)
    merged = load_env_values_from_text(base_path.read_text(encoding="utf-8"))
    merged.update(profile)
    out: list[str] = []
    for raw_line in base_path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.strip().startswith("#"):
            out.append(raw_line)
            continue
        match = _ENV_LINE.match(raw_line)
        if match and match.group(2) in merged:
            key = match.group(2)
            out.append(f"{key}={format_env_value(merged[key])}")
        else:
            out.append(raw_line)
    return "\n".join(out).rstrip() + "\n"


def write_windows_minipc_example(
    base_path: Path = Path(".env.example"),
    output_path: Path = Path(".env.windows-minipc.example"),
) -> Path:
    header = [
        "# Template ottimizzato per mini PC Windows.",
        "# Copia in .env e completa credenziali/segreti, oppure usa:",
        "#   .\\scripts\\install_windows.ps1",
        "# Il wizard applica automaticamente questo profilo di tuning.",
        "",
    ]
    body = build_example_content(base_path, MINI_PC_WINDOWS)
    # Sostituisce il blocco header iniziale di .env.example con quello Windows.
    lines = body.splitlines()
    first_key_idx = next(
        (idx for idx, line in enumerate(lines) if _ENV_LINE.match(line)),
        0,
    )
    content = "\n".join(header + lines[first_key_idx:]).rstrip() + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Applica un profilo di tuning al file .env (senza toccare le credenziali)."
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Percorso del file .env (default: ./.env)",
    )
    parser.add_argument(
        "--profile",
        default="mini-pc-windows",
        choices=sorted(PROFILES),
        help="Profilo da applicare",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Elenca i profili disponibili",
    )
    parser.add_argument(
        "--write-windows-example",
        metavar="PATH",
        nargs="?",
        const=".env.windows-minipc.example",
        help="Genera .env.windows-minipc.example da .env.example + profilo mini-pc-windows",
    )
    args = parser.parse_args(argv)

    if args.write_windows_example is not None:
        out = write_windows_minipc_example(output_path=Path(args.write_windows_example))
        print(f"Scritto {out} ({len(MINI_PC_WINDOWS)} chiavi di tuning mini PC).")
        return 0

    if args.list:
        for name in sorted(PROFILES):
            print(f"{name}: {len(PROFILES[name])} chiavi")
        return 0

    path = Path(args.env_file)
    if not path.is_file():
        print(f"File non trovato: {path}", file=sys.stderr)
        return 1

    updated = apply_profile(path, args.profile)
    print(f"Profilo '{args.profile}' applicato a {path} ({len(updated)} chiavi).")
    for key in updated:
        print(f"  - {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
