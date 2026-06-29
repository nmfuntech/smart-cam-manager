"""Percorsi runtime: supporta installazione Windows con dati in ProgramData."""

from __future__ import annotations

import os
from pathlib import Path

INSTALLED_MARKER = ".installed"


def installed_data_home() -> Path | None:
    """Ritorna ProgramData\\BLACKFRAME se l'app è stata installata con Inno Setup."""
    explicit = os.getenv("BLACKFRAME_HOME", "").strip()
    if explicit:
        return Path(explicit)
    if os.name != "nt":
        return None
    program_data = os.getenv("PROGRAMDATA", "").strip()
    if not program_data:
        return None
    home = Path(program_data) / "BLACKFRAME"
    if (home / INSTALLED_MARKER).is_file():
        return home
    return None


def configure_runtime_environment() -> Path:
    """Imposta cwd e .env per installazione Windows; altrimenti solo load_dotenv()."""
    from dotenv import load_dotenv

    home = installed_data_home()
    if home is None:
        load_dotenv()
        return Path.cwd()

    home.mkdir(parents=True, exist_ok=True)
    (home / "captures").mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    load_dotenv(home / ".env")
    return home


def runtime_python(root: Path) -> Path:
    """Python da usare per servizio/launcher (bundled o .venv)."""
    bundled = root / "runtime" / "python" / "python.exe"
    if bundled.is_file():
        return bundled
    venv = root / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return venv
    return Path(os.environ.get("BLACKFRAME_PYTHON", "python"))
