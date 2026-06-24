#!/usr/bin/env python3
"""Verifica prerequisiti runtime di BLACKFRAME (ffmpeg, modelli, tuning .env)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    return ROOT / os.getenv(name, default).strip()


def check_ffmpeg() -> list[str]:
    issues: list[str] = []
    if shutil.which("ffmpeg") is None:
        issues.append(
            "ffmpeg non trovato in PATH: le clip MP4 restano in codec mp4v e non "
            "si riproducono nel browser. Installa ffmpeg e riavvia il terminale."
        )
    if shutil.which("ffprobe") is None:
        issues.append(
            "ffprobe non trovato in PATH: la transcodifica/verifica clip non è disponibile."
        )
    return issues


def check_classification_models() -> list[str]:
    if not _env_flag("CLASSIFICATION_ENABLED"):
        return []

    backend = os.getenv("CLASSIFICATION_BACKEND", "local").strip().lower()
    if backend != "detection":
        return []

    model = _env_path(
        "CLASSIFICATION_DETECTION_MODEL_PATH",
        "models/ssd_mobilenet_v2_coco.pb",
    )
    config = _env_path(
        "CLASSIFICATION_DETECTION_CONFIG_PATH",
        "models/ssd_mobilenet_v2_coco.pbtxt",
    )
    missing = [str(p.relative_to(ROOT)) for p in (model, config) if not p.is_file()]
    if not missing:
        return []
    return [
        "Classificazione detection abilitata ma mancano i modelli: "
        + ", ".join(missing)
        + ". Esegui: make fetch-model"
    ]


def check_motion_tuning() -> list[str]:
    issues: list[str] = []
    try:
        min_area = int(os.getenv("MOTION_MIN_AREA", "600"))
    except ValueError:
        min_area = 600
    if min_area > 4000:
        issues.append(
            f"MOTION_MIN_AREA={min_area} è molto alto: rischi di non rilevare soggetti "
            "piccoli. Valori consigliati su mini PC: 1500–2500."
        )
    stream = os.getenv("TAPO_STREAM_PATH", "stream1").strip()
    if sys.platform == "win32" and stream == "stream1":
        issues.append(
            "TAPO_STREAM_PATH=stream1 su Windows/mini PC: il flusso HD satura la CPU. "
            "Prova stream2 (sottostream SD) o esegui: "
            "poetry run python scripts/env_profiles.py --profile mini-pc-windows"
        )
    return issues


def run_checks() -> tuple[list[str], list[str]]:
    """Ritorna (errori, avvisi). Gli errori bloccano --strict."""
    errors: list[str] = []
    warnings: list[str] = []

    ffmpeg_issues = check_ffmpeg()
    if ffmpeg_issues:
        errors.extend(ffmpeg_issues)

    model_issues = check_classification_models()
    if model_issues:
        errors.extend(model_issues)

    warnings.extend(check_motion_tuning())
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Esci con codice 1 se mancano prerequisiti obbligatori",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Stampa solo errori/avvisi (niente OK)",
    )
    args = parser.parse_args(argv)

    env_path = ROOT / ".env"
    if env_path.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except ImportError:
            pass

    errors, warnings = run_checks()

    if not args.quiet and not errors and not warnings:
        print("Prerequisiti OK (ffmpeg, modelli, tuning base).")
        return 0

    for message in errors:
        print(f"ERRORE: {message}")
    for message in warnings:
        print(f"AVVISO: {message}")

    if errors and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
