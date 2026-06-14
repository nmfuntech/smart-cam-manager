#!/usr/bin/env python3
"""Helper per configurare le notifiche Telegram di BLACKFRAME.

Flusso tipico (dopo aver creato il bot con @BotFather):

    # 1. Scrivi un messaggio qualsiasi al tuo bot dall'app Telegram, poi:
    poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --discover

    # 2. Trovato il chat_id, invia un messaggio di prova:
    poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --chat-id <CHAT_ID> --test

    # 3. Se tutto ok, salva le credenziali in .env e abilita le notifiche:
    poetry run python -m scripts.telegram_setup --token <BOT_TOKEN> --chat-id <CHAT_ID> --write-env

In assenza di --token/--chat-id i valori vengono letti dalle variabili
d'ambiente NOTIFY_TELEGRAM_BOT_TOKEN / NOTIFY_TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from notifications import discover_telegram_chats, send_telegram_test

DEFAULT_ENV_PATH = Path(".env")


def discover_chat_id(token: str) -> int:
    print("Cerco le chat recenti del bot (getUpdates)...")
    ok, chats, error = discover_telegram_chats(token)
    if not ok:
        print(f"✗ Token non valido o errore: {error}", file=sys.stderr)
        return 1
    if not chats:
        print(
            "✗ Nessuna chat trovata. Apri Telegram, cerca il tuo bot e inviagli\n"
            "  un messaggio qualsiasi (es. /start), poi rilancia con --discover.",
            file=sys.stderr,
        )
        return 1
    print("✓ Chat trovate:")
    for chat in chats:
        print(f"    chat_id={chat['chat_id']}  ->  {chat['label']}")
    if len(chats) == 1:
        print(f"\nUsa: --chat-id {chats[0]['chat_id']}")
    return 0


def send_test(token: str, chat_id: str) -> int:
    print(f"Invio messaggio di prova a chat_id={chat_id}...")
    ok, error = send_telegram_test(token, chat_id)
    if ok:
        print("✓ Messaggio inviato. Controlla Telegram.")
        return 0
    print(f"✗ Invio fallito: {error}", file=sys.stderr)
    return 1


def write_env(token: str, chat_id: str, env_path: Path) -> int:
    updates = {
        "NOTIFY_TELEGRAM_ENABLED": "true",
        "NOTIFY_TELEGRAM_BOT_TOKEN": token,
        "NOTIFY_TELEGRAM_CHAT_ID": chat_id,
    }
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ Scritto {env_path}: notifiche abilitate. Riavvia l'app (make run).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup notifiche Telegram BLACKFRAME")
    parser.add_argument("--token", default=os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", ""))
    parser.add_argument("--chat-id", default=os.getenv("NOTIFY_TELEGRAM_CHAT_ID", ""))
    parser.add_argument(
        "--discover", action="store_true", help="Ricava il chat_id dagli update del bot"
    )
    parser.add_argument("--test", action="store_true", help="Invia un messaggio di prova")
    parser.add_argument(
        "--write-env", action="store_true", help="Salva token+chat_id in .env e abilita"
    )
    parser.add_argument("--env-path", default=str(DEFAULT_ENV_PATH))
    args = parser.parse_args()

    token = args.token.strip()
    chat_id = args.chat_id.strip()

    if not (args.discover or args.test or args.write_env):
        parser.error("specifica almeno una azione: --discover, --test o --write-env")
    if not token:
        parser.error("token mancante: passa --token o imposta NOTIFY_TELEGRAM_BOT_TOKEN")

    if args.discover:
        rc = discover_chat_id(token)
        if rc != 0:
            return rc
    if args.test or args.write_env:
        if not chat_id:
            parser.error("chat_id mancante: passa --chat-id o imposta NOTIFY_TELEGRAM_CHAT_ID")
    if args.test:
        rc = send_test(token, chat_id)
        if rc != 0:
            return rc
    if args.write_env:
        return write_env(token, chat_id, Path(args.env_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
