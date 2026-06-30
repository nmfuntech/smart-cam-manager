#!/usr/bin/env python3
"""PoC Fase 0: accende/spegne una lampada Tuya in LAN usando il driver del progetto.

Serve a verificare, prima di qualsiasi integrazione, che i dati estratti col
wizard tinytuya (device_id + local_key + ip + version) controllino davvero il
device sulla rete locale. Riusa automation.devices.TuyaLanDevice, così collaudi
esattamente il codice che userà l'automazione, non un percorso alternativo.

Esempi:
    poetry run python scripts/tuya_poc.py --device-id bfa1b2c3... --ip 192.168.1.10 \
        --local-key abcd1234 --version 3.4 --cycle

    # solo accendere / solo spegnere
    poetry run python scripts/tuya_poc.py ... --on
    poetry run python scripts/tuya_poc.py ... --off

Trova device_id/local_key con:  python -m tinytuya wizard
Trova ip/version con:           python -m tinytuya scan
"""

import argparse
import sys
import time

from blackframe.automation.devices import DeviceError, TuyaLanDevice


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PoC controllo Tuya LAN (accendi/spegni).")
    parser.add_argument("--device-id", required=True, help="ID device (dal wizard tinytuya)")
    parser.add_argument("--ip", required=True, help="IP del device in LAN (dallo scan)")
    parser.add_argument("--local-key", required=True, help="local_key del device (dal wizard)")
    parser.add_argument(
        "--version",
        type=float,
        default=3.3,
        help="Versione protocollo Tuya (3.1/3.3/3.4/3.5, vedi scan). Default 3.3",
    )
    parser.add_argument("--name", default="poc", help="Nome logico per i log")
    parser.add_argument(
        "--switch-dp",
        type=int,
        default=1,
        help="DP dell'interruttore on/off: 1 per le prese (default), 20 per le lampade RGBCW",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--on", action="store_true", help="Accende e basta")
    action.add_argument("--off", action="store_true", help="Spegne e basta")
    action.add_argument(
        "--cycle",
        action="store_true",
        help="Accende, attende --delay secondi, spegne (default se non specifichi nulla)",
    )
    parser.add_argument("--delay", type=float, default=2.0, help="Pausa nel --cycle (s)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    device = TuyaLanDevice(
        name=args.name,
        device_id=args.device_id,
        ip=args.ip,
        local_key=args.local_key,
        version=args.version,
        switch_dp=args.switch_dp,
    )

    try:
        if args.on:
            device.turn_on()
            print(f"[OK] {args.name}: acceso")
        elif args.off:
            device.turn_off()
            print(f"[OK] {args.name}: spento")
        else:  # default: cycle
            device.turn_on()
            print(f"[OK] {args.name}: acceso — attendo {args.delay}s...")
            time.sleep(args.delay)
            device.turn_off()
            print(f"[OK] {args.name}: spento")
    except DeviceError as exc:
        print(f"[ERRORE] {exc}", file=sys.stderr)
        print(
            "Verifica: stessa LAN del device? local_key aggiornata? --version corretta "
            "(prova lo scan)? firewall che blocca UDP/6668?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
