#!/usr/bin/env python3
"""BLACKFRAME CLI — comandi utili per gestire un'istanza BLACKFRAME da terminale.

Configurazione (variabili d'ambiente o .env nella directory del progetto):
  BLACKFRAME_URL       URL base dell'app (default: http://localhost:8000)
  BLACKFRAME_PASSWORD  password admin

Esempi:
  bf status
  bf events --limit 5 --category persona
  bf config
  bf config set MOTION_THRESHOLD 500
  bf motion off
  bf classify on
  bf notify off
  bf record on
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests non trovato. Esegui: poetry install")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return os.getenv("BLACKFRAME_URL", "http://localhost:8000").rstrip("/")


def _password() -> str:
    pw = os.getenv("BLACKFRAME_PASSWORD", "")
    if pw:
        return pw
    for candidate in [Path(".env"), Path(__file__).parent.parent / ".env"]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("APP_ADMIN_PASSWORD=") and not line.startswith("#"):
                    return line.split("=", 1)[1].strip()
    return ""


_SESSION_FILE = Path.home() / ".cache" / "blackframe" / "session.json"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class BFClient:
    def __init__(self, base_url: str, password: str):
        self.base_url = base_url
        self.password = password
        self._session = requests.Session()
        self._csrf_token: str | None = None
        self._load_session()

    # --- session persistence ---

    def _load_session(self) -> None:
        if not _SESSION_FILE.exists():
            return
        try:
            data = json.loads(_SESSION_FILE.read_text())
            for name, value in data.get("cookies", {}).items():
                self._session.cookies.set(name, value)
            self._csrf_token = data.get("csrf_token")
        except Exception:
            pass

    def _save_session(self) -> None:
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(
            json.dumps(
                {
                    "cookies": dict(self._session.cookies),
                    "csrf_token": self._csrf_token,
                }
            )
        )

    # --- auth ---

    def _login(self) -> None:
        if not self.password:
            sys.exit(
                "Password non configurata.\n"
                "Imposta BLACKFRAME_PASSWORD oppure APP_ADMIN_PASSWORD nel file .env"
            )
        resp = self._session.post(
            f"{self.base_url}/login",
            data={"password": self.password},
            allow_redirects=True,
            timeout=10,
        )
        if "/login" in resp.url and resp.status_code == 200:
            sys.exit("Login fallito: password errata.")
        self._save_session()

    def _ensure_csrf(self) -> None:
        if self._csrf_token:
            return
        resp = self._session.get(f"{self.base_url}/api/csrf_token", timeout=10)
        if resp.status_code == 401:
            self._login()
            resp = self._session.get(f"{self.base_url}/api/csrf_token", timeout=10)
        resp.raise_for_status()
        self._csrf_token = resp.json()["csrf_token"]
        self._save_session()

    # --- request helpers ---

    def get(self, path: str) -> dict:
        resp = self._session.get(f"{self.base_url}{path}", timeout=10)
        if resp.status_code == 401:
            self._login()
            resp = self._session.get(f"{self.base_url}{path}", timeout=10)
        resp.raise_for_status()
        return resp.json()

    def patch(self, path: str, data: dict) -> dict:
        self._ensure_csrf()
        headers = {"X-CSRF-Token": self._csrf_token}
        resp = self._session.patch(f"{self.base_url}{path}", json=data, headers=headers, timeout=10)
        if resp.status_code == 401:
            self._csrf_token = None
            self._ensure_csrf()
            headers["X-CSRF-Token"] = self._csrf_token
            resp = self._session.patch(
                f"{self.base_url}{path}", json=data, headers=headers, timeout=10
            )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _ok(label: str, value: str) -> None:
    print(f"  {label:<28} {value}")


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * (len(title)))


def _bool_icon(v: object) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return ts


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_status(client: BFClient, _args: argparse.Namespace) -> None:
    try:
        health = client._session.get(f"{client.base_url}/health", timeout=5)
        alive = health.status_code == 200
    except Exception:
        alive = False

    if not alive:
        sys.exit(f"App non raggiungibile su {client.base_url}")

    stream = client.get("/stream_status")
    motion = client.get("/motion_status")

    _section("Stream")
    _ok("connesso", _bool_icon(stream.get("connected", False)))
    _ok("frame ricevuti", str(stream.get("frames_received", 0)))
    _ok("errori consecutivi", str(stream.get("consecutive_errors", 0)))

    _section("Motion detection")
    _ok("abilitato", _bool_icon(motion.get("enabled", False)))
    _ok("in moto", _bool_icon(motion.get("motion_detected", False)))
    _ok("frame processati", str(motion.get("processed_frames", 0)))
    _ok("streak trigger", str(motion.get("trigger_streak", 0)))

    cls = motion.get("classifier", {})
    if cls:
        _section("Classificazione")
        _ok("abilitata", _bool_icon(cls.get("enabled", False)))
        targets = cls.get("targets", [])
        _ok("target", ", ".join(targets) if targets else "nessuno")
    print()


def cmd_events(client: BFClient, args: argparse.Namespace) -> None:
    limit = args.limit
    data = client.get(f"/motion_captures?limit={limit}")
    events = data.get("captures", [])
    total = data.get("total", 0)

    if args.category:
        events = [e for e in events if e.get("category") == args.category]

    if not events:
        print("Nessun evento trovato.")
        return

    print(f"\n{'ID':<30} {'Categoria':<22} {'Inizio':<20} {'Fine':<20}")
    print("-" * 96)
    for ev in events:
        eid = ev.get("event_id", "—")
        cat = ev.get("category") or ev.get("class_label") or "—"
        start = _fmt_ts(ev.get("start_time"))
        end = _fmt_ts(ev.get("end_time"))
        print(f"{eid:<30} {cat:<22} {start:<20} {end:<20}")
    print(f"\n{len(events)} mostrati / {total} totali")


def cmd_config(client: BFClient, args: argparse.Namespace) -> None:
    if args.set_key:
        _cmd_config_set(client, args.set_key, args.set_value)
        return

    data = client.get("/runtime_config")
    cfg = data.get("config", {})
    _section("Runtime config")
    for k, v in sorted(cfg.items()):
        _ok(k, str(v))
    print()


def _cmd_config_set(client: BFClient, key: str, value: str) -> None:
    result = client.patch("/api/runtime_config", {"updates": {key: value}})
    if result.get("ok"):
        new_val = result.get("config", {}).get(key, value)
        print(f"{key} = {new_val}")
    else:
        sys.exit(f"Errore: {result.get('error', 'sconosciuto')}")


def _toggle(client: BFClient, key: str, state: str) -> None:
    value = "true" if state == "on" else "false"
    result = client.patch("/api/runtime_config", {"updates": {key: value}})
    if result.get("ok"):
        new_val = result.get("config", {}).get(key, value)
        label = "on" if str(new_val).lower() in {"true", "1", "yes"} else "off"
        print(f"{key}: {label}")
    else:
        sys.exit(f"Errore: {result.get('error', 'sconosciuto')}")


def cmd_motion(client: BFClient, args: argparse.Namespace) -> None:
    _toggle(client, "MOTION_ENABLED", args.state)


def cmd_classify(client: BFClient, args: argparse.Namespace) -> None:
    _toggle(client, "CLASSIFICATION_ENABLED", args.state)


def cmd_notify(client: BFClient, args: argparse.Namespace) -> None:
    _toggle(client, "NOTIFY_TELEGRAM_ENABLED", args.state)


def cmd_record(client: BFClient, args: argparse.Namespace) -> None:
    _toggle(client, "RECORD_ENABLED", args.state)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bf",
        description="BLACKFRAME CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--url", default=None, help="URL base (default: BLACKFRAME_URL o localhost:8000)"
    )

    sub = p.add_subparsers(dest="command", metavar="comando")
    sub.required = True

    sub.add_parser("status", help="Stato stream, motion e classificazione")

    ev = sub.add_parser("events", help="Elenco eventi di moto recenti")
    ev.add_argument("--limit", type=int, default=10, help="Numero massimo di eventi (default: 10)")
    ev.add_argument(
        "--category",
        choices=["persona", "animale_domestico", "movimento"],
        help="Filtra per categoria",
    )

    cfg = sub.add_parser("config", help="Leggi o modifica la runtime config")
    cfg.add_argument(
        "set_key", nargs="?", metavar="KEY", help="Chiave da modificare (es. MOTION_THRESHOLD)"
    )
    cfg.add_argument("set_value", nargs="?", metavar="VALUE", help="Nuovo valore")

    for name, help_text in [
        ("motion", "Abilita/disabilita motion detection"),
        ("classify", "Abilita/disabilita classificazione persona/animale"),
        ("notify", "Abilita/disabilita notifiche Telegram"),
        ("record", "Abilita/disabilita registrazione eventi"),
    ]:
        s = sub.add_parser(name, help=help_text)
        s.add_argument("state", choices=["on", "off"])

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    url = args.url or _base_url()
    # No --password flag: a password on argv leaks via `ps`/shell history. Read it
    # from BLACKFRAME_PASSWORD / .env, falling back to a hidden interactive prompt.
    password = _password() or getpass("Password admin BLACKFRAME: ")
    client = BFClient(url, password)

    dispatch = {
        "status": cmd_status,
        "events": cmd_events,
        "config": cmd_config,
        "motion": cmd_motion,
        "classify": cmd_classify,
        "notify": cmd_notify,
        "record": cmd_record,
    }

    try:
        dispatch[args.command](client, args)
    except requests.exceptions.ConnectionError:
        sys.exit(f"Impossibile connettersi a {url}")
    except requests.exceptions.HTTPError as e:
        sys.exit(f"Errore HTTP {e.response.status_code}: {e.response.text[:200]}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
