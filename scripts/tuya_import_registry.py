#!/usr/bin/env python3
"""Importa nel registry cifrato i device da output tinytuya (scan/wizard).

Legge ``devices.json`` (e opzionalmente ``snapshot.json`` per il DP switch),
converte ogni device raggiungibile in LAN e lo salva cifrato in
``data/tuya_devices.json``.

Esempi:
    # dopo wizard + scan
    poetry run python scripts/tuya_import_registry.py

    # anteprima senza scrivere
    poetry run python scripts/tuya_import_registry.py --dry-run

    # lancia scan tinytuya e poi importa
    poetry run python scripts/tuya_import_registry.py --scan

    # nomi logici custom (Smart Life → rules.yaml)
    poetry run python scripts/tuya_import_registry.py --map config/automation/tuya_device_names.yaml

Flusso consigliato (Fase 0):
    poetry run python -m tinytuya wizard
    poetry run python -m tinytuya scan
    poetry run python scripts/tuya_import_registry.py --map config/automation/tuya_device_names.yaml
    poetry run python scripts/tuya_import_registry.py --dry-run  # verifica
    poetry run python scripts/tuya_import_registry.py --map config/automation/tuya_device_names.yaml
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from dotenv import load_dotenv

from blackframe.automation.registry import DeviceRegistry
from blackframe.automation.tuya_import import (
    build_registry_payloads,
    load_name_map,
    load_snapshot_by_id,
    load_tinytuya_devices,
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Importa device Tuya (devices.json) nel registry cifrato BLACKFRAME."
    )
    parser.add_argument(
        "--device-file",
        default="devices.json",
        help="JSON tinytuya con id/key/ip (default: devices.json nella cwd)",
    )
    parser.add_argument(
        "--snapshot-file",
        default="snapshot.json",
        help="JSON snapshot tinytuya per inferire switch_dp (default: snapshot.json)",
    )
    parser.add_argument(
        "--map",
        dest="name_map",
        metavar="FILE",
        help=(
            "YAML/JSON: nome Smart Life → nome logico "
            "(es. config/automation/tuya_device_names.yaml)"
        ),
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="Percorso registry (default: AUTOMATION_DEVICES_PATH o data/tuya_devices.json)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Esegue 'python -m tinytuya scan -yes' prima dell'import",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra cosa verrebbe salvato senza scrivere sul registry",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="Importa solo questi nomi logici (ripetibile)",
    )
    return parser.parse_args(argv)


def run_tinytuya_scan() -> None:
    print("Scan rete locale (tinytuya)...")
    subprocess.run(
        [sys.executable, "-m", "tinytuya", "scan", "-yes"],
        check=True,
    )


def main(argv=None) -> int:
    load_dotenv()
    args = parse_args(argv)

    if args.scan:
        run_tinytuya_scan()

    try:
        scan_devices = load_tinytuya_devices(args.device_file)
        name_map = load_name_map(args.name_map)
        snapshot_by_id = load_snapshot_by_id(args.snapshot_file)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        return 1

    payloads, skipped = build_registry_payloads(
        scan_devices,
        snapshot_by_id=snapshot_by_id,
        name_map=name_map,
    )

    if args.only:
        allowed = {name.strip() for name in args.only if name.strip()}
        payloads = [p for p in payloads if p["name"] in allowed]
        if not payloads:
            print("[ERRORE] Nessun device corrisponde a --only", file=sys.stderr)
            return 1

    if skipped:
        print("Saltati:")
        for line in skipped:
            print(f"  - {line}")

    if not payloads:
        print(
            "[ERRORE] Nessun device importabile (tutti offline o dati incompleti)",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print("Anteprima import (dry-run):")
        for payload in payloads:
            redacted = {**payload, "local_key": "***"}
            print(f"  {redacted}")
        print(f"\n{len(payloads)} device pronti per il registry.")
        return 0

    store_path = args.store_path or os.getenv("AUTOMATION_DEVICES_PATH", "data/tuya_devices.json")
    registry = DeviceRegistry(store_path=store_path)
    saved = []
    for payload in payloads:
        saved.append(registry.save_device(payload))
        print(f"[OK] {payload['name']} ← {payload['ip']} (dp {payload['switch_dp']})")

    print(f"\nRegistry aggiornato: {len(saved)} device.")
    print(registry.list_devices())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
