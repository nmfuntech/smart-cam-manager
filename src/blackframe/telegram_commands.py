"""Telegram command polling for BLACKFRAME.

The command bot uses Telegram long polling, so the app does not need a public
webhook URL. It is disabled by default and only accepts exact configured chat IDs.
"""

from __future__ import annotations

import hmac
import json
import logging
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from blackframe import audit, telegram_client, telegram_invites
from blackframe.commands import COMMAND_REGISTRY, CommandResult
from blackframe.commands import execute as registry_execute
from blackframe.envutil import env_bool as _env_bool
from blackframe.envutil import env_float, env_int
from blackframe.envutil import env_str as _env
from blackframe.recording import record_clip
from blackframe.service_layer import _write_private_text

# Clip on-demand: durata di default e limite massimo (secondi).
CLIP_DEFAULT_SEC = 10
CLIP_MAX_SEC = 30

logger = logging.getLogger(__name__)

# Comandi raggruppati per categoria. Unica fonte: il menu Telegram (COMMANDS) e
# /help (_help_text) sono entrambi derivati da qui.
HELP_SECTIONS = [
    (
        "ℹ️ Stato",
        [
            ("status", "Stato camera e movimento"),
            ("config", "Riepilogo impostazioni"),
            ("snapshot", "Invia foto live"),
            ("clip", "Registra e invia clip (default 10s)"),
            ("latest", "Invia ultimo evento"),
            ("events", "Elenca ultimi eventi"),
        ],
    ),
    (
        "🛡 Modalità",
        [
            ("modalita", "Mostra la modalità attuale"),
            ("arma", "Armato: rilevamento e avvisi"),
            ("casa", "Casa: registra ma senza avvisi"),
            ("disarma", "Disarmato: rilevamento in pausa"),
        ],
    ),
    (
        "👁 Rilevamento",
        [
            ("motion_on", "Attiva rilevamento"),
            ("motion_off", "Disattiva rilevamento"),
            ("sensitivity", "Sensibilita: bassa|media|alta"),
            ("classification_on", "Attiva riconoscimento"),
            ("classification_off", "Disattiva riconoscimento"),
            ("detect_person_on", "Notifica persone"),
            ("detect_person_off", "Ignora persone"),
            ("detect_pet_on", "Notifica animali"),
            ("detect_pet_off", "Ignora animali"),
        ],
    ),
    (
        "🔔 Notifiche",
        [
            ("notifications_on", "Attiva notifiche"),
            ("notifications_off", "Disattiva notifiche"),
            ("mute", "Silenzia per N minuti"),
            ("resume", "Riprendi notifiche"),
        ],
    ),
    (
        "⏺ Registrazione",
        [
            ("record_on", "Attiva clip evento"),
            ("record_off", "Disattiva clip evento"),
            ("continuous_on", "Attiva registrazione continua"),
            ("continuous_off", "Disattiva registrazione continua"),
        ],
    ),
    (
        "🕹 PTZ",
        [
            ("ptz_left", "Muovi a sinistra"),
            ("ptz_right", "Muovi a destra"),
            ("ptz_up", "Muovi in alto"),
            ("ptz_down", "Muovi in basso"),
            ("ptz_stop", "Ferma PTZ"),
            ("ptz_home", "PTZ home"),
        ],
    ),
    (
        "🏠 Domotica",
        [
            ("devices", "Elenca dispositivi"),
            ("device_on", "Accendi dispositivo: <nome>"),
            ("device_off", "Spegni dispositivo: <nome>"),
            ("rules", "Elenca regole"),
            ("rule_run", "Esegui regola ora: <nome>"),
            ("rule_on", "Abilita regola: <nome>"),
            ("rule_off", "Disabilita regola: <nome>"),
        ],
    ),
]

# Menu Telegram (setMyCommands): tutti i comandi delle sezioni + /help.
COMMANDS = [pair for _, commands in HELP_SECTIONS for pair in commands]
COMMANDS.append(("menu", "Bottoni rapidi"))
COMMANDS.append(("help", "Mostra comandi"))

# Reply keyboard persistente: comandi principali sempre sotto la barra di input.
# Le etichette non sono /comandi, quindi vengono mappate al comando reale.
MAIN_KEYBOARD_ROWS = [
    ["📊 Stato", "📸 Snapshot"],
    ["🎬 Clip", "🛡 Modalità"],
    ["📋 Menu"],
]
REPLY_BUTTON_COMMANDS = {
    "🛡 Modalità": "/modalita",
    "📊 Stato": "/status",
    "📸 Snapshot": "/snapshot",
    "🎬 Clip": "/clip",
    "📋 Menu": "/menu",
}

# Inline keyboard del /menu: tutto il resto. callback_data = comando (+ args).
INLINE_MENU_ROWS = [
    [("🛡 Armato", "/arma"), ("🏠 Casa", "/casa"), ("💤 Disarmato", "/disarma")],
    [("👁 Movimento ON", "/motion_on"), ("👁 Movimento OFF", "/motion_off")],
    [
        ("🎚 Sens. bassa", "/sensitivity bassa"),
        ("🎚 Media", "/sensitivity media"),
        ("🎚 Alta", "/sensitivity alta"),
    ],
    [("🧠 Riconosc. ON", "/classification_on"), ("🧠 OFF", "/classification_off")],
    [("🧍 Persone ON", "/detect_person_on"), ("🧍 OFF", "/detect_person_off")],
    [("🐾 Animali ON", "/detect_pet_on"), ("🐾 OFF", "/detect_pet_off")],
    [("🔔 Notifiche ON", "/notifications_on"), ("🔕 OFF", "/notifications_off")],
    [("⏸ Pausa 15m", "/mute 15"), ("▶️ Riprendi", "/resume")],
    [("⏺ Clip evento ON", "/record_on"), ("⏺ OFF", "/record_off")],
    [("🔁 Continua ON", "/continuous_on"), ("🔁 OFF", "/continuous_off")],
    [("🎬 Clip 5s", "/clip 5"), ("🎬 10s", "/clip 10"), ("🎬 30s", "/clip 30")],
    [
        ("⬅️", "/ptz_left"),
        ("⬆️", "/ptz_up"),
        ("⬇️", "/ptz_down"),
        ("➡️", "/ptz_right"),
    ],
    [("⏹ Stop PTZ", "/ptz_stop"), ("🏠 Home", "/ptz_home")],
    [("🗂 Eventi", "/events"), ("🖼 Ultimo", "/latest")],
]


# Pavimenti di default specifici di questo modulo; il parsing vive in envutil.
def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    return env_float(name, default, minimum=minimum)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    return env_int(name, default, minimum=minimum)


def _reply_keyboard_markup() -> str:
    return json.dumps(
        {
            "keyboard": [[{"text": text} for text in row] for row in MAIN_KEYBOARD_ROWS],
            "resize_keyboard": True,
            "is_persistent": True,
        }
    )


def _inline_menu_markup() -> str:
    return json.dumps(
        {
            "inline_keyboard": [
                [{"text": label, "callback_data": command} for label, command in row]
                for row in INLINE_MENU_ROWS
            ]
        }
    )


def _bot_api_call(
    token: str,
    method: str,
    params: dict | None = None,
    timeout: float = 15,
) -> dict:
    return telegram_client.api_call(token, method, params, timeout=timeout)


class PerChatExecutor:
    """Run updates concurrently across chats, in order within each chat.

    The polling loop used to execute every command inline: one slow call (an
    LLM answer on the mini PC, a photo upload on a bad connection, an ONVIF
    timeout) froze the bot for everyone. Now the loop only enqueues; a small
    pool drains one queue per chat, so order is kept per user.
    """

    def __init__(self, max_workers: int = 3, max_queue: int = 20) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tg-cmd")
        self._lock = threading.Lock()
        self._queues: dict[str, deque] = {}
        self._active: set[str] = set()
        self._max_queue = max_queue

    def submit(self, key: str, fn) -> bool:
        with self._lock:
            queue = self._queues.setdefault(key, deque())
            if len(queue) >= self._max_queue:
                return False
            queue.append(fn)
            if key in self._active:
                return True
            self._active.add(key)
        self._pool.submit(self._drain, key)
        return True

    def _drain(self, key: str) -> None:
        while True:
            with self._lock:
                queue = self._queues.get(key)
                if not queue:
                    self._active.discard(key)
                    self._queues.pop(key, None)
                    return
                fn = queue.popleft()
            try:
                fn()
            except Exception:
                logger.exception("Gestione comando Telegram fallita")


class TelegramCommandBot:
    def __init__(self, services: Any):
        self.services = services
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_update_id: int | None = None
        self._rate_events: dict[str, deque[float]] = defaultdict(deque)
        self._menu_configured = False
        self._guests_lock = threading.Lock()
        self._bot_username: str | None = None
        self._bot_username_token: str = ""
        self._clip_lock = threading.Lock()
        self._active_clips = 0
        self._executor: PerChatExecutor | None = None
        # The Telegram user behind the update being handled (per worker thread):
        # in group chats the chat id alone does not identify who pressed a button.
        self._ctx = threading.local()

    @property
    def enabled(self) -> bool:
        return _env_bool("TELEGRAM_COMMANDS_ENABLED", False)

    def _token(self) -> str:
        return _env("NOTIFY_TELEGRAM_BOT_TOKEN")

    def _admin_chat_ids(self) -> set[str]:
        raw = _env("TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS") or _env("NOTIFY_TELEGRAM_CHAT_ID")
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _guests_file(self) -> Path:
        path = _env("TELEGRAM_GUESTS_FILE")
        return Path(path) if path else Path("data/telegram_guests.json")

    def _load_guests(self) -> dict[str, dict]:
        try:
            data = json.loads(self._guests_file().read_text(encoding="utf-8"))
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_guests(self, guests: dict[str, dict]) -> None:
        # The guest list holds authorized Telegram chat IDs and names: write it
        # 0o600 (atomic temp + replace) so other local users cannot read it.
        _write_private_text(
            self._guests_file(),
            json.dumps(guests, indent=2, ensure_ascii=False),
        )

    def _allowed_chat_ids(self) -> set[str]:
        ids = self._admin_chat_ids()
        with self._guests_lock:
            ids |= set(self._load_guests().keys())
        return ids

    def _is_admin(self, chat_id: str) -> bool:
        if chat_id not in self._admin_chat_ids():
            return False
        if not chat_id.startswith("-"):
            return True  # private chat: chat id == user id
        # Group chat: every member shares the chat id. When admin user ids are
        # configured, only those users get admin rights inside the group.
        admin_users = {
            item.strip() for item in _env("TELEGRAM_ADMIN_USER_IDS").split(",") if item.strip()
        }
        if not admin_users:
            return True
        return str(getattr(self._ctx, "user_id", "") or "") in admin_users

    def _guest_role(self, chat_id: str) -> str | None:
        with self._guests_lock:
            info = self._load_guests().get(chat_id)
        if info is None:
            return None
        role = info.get("role") or telegram_invites.ROLE_FAMILY
        return role if role in telegram_invites.ROLES else telegram_invites.ROLE_FAMILY

    def _actor(self, chat_id: str) -> str:
        if chat_id in self._admin_chat_ids():
            return f"admin:{chat_id}"
        with self._guests_lock:
            info = self._load_guests().get(chat_id) or {}
        return f"{info.get('name') or 'ospite'}:{chat_id}"

    def configured(self) -> bool:
        return bool(self.enabled and self._token() and self._allowed_chat_ids())

    def start(self) -> None:
        if not self.enabled:
            logger.info("Comandi Telegram disabilitati")
            return
        if not self.configured():
            logger.warning("Comandi Telegram non configurati: token o chat_id mancanti")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def status(self) -> dict:
        running = self._thread is not None and self._thread.is_alive()
        allowed = self._allowed_chat_ids()
        return {
            "enabled": self.enabled,
            "configured": bool(self.enabled and self._token() and allowed),
            "running": running,
            "allowed_chat_count": len(allowed),
        }

    def _run(self) -> None:
        self._forget_pending_updates()
        self._set_commands_menu()
        if self._executor is None:
            self._executor = PerChatExecutor(
                max_workers=_env_int("TELEGRAM_COMMANDS_WORKERS", 3, 1)
            )
        failures = 0
        while not self._stop.is_set():
            token = self._token()
            if not self.enabled or not token or not self._allowed_chat_ids():
                time.sleep(5)
                continue

            if self._bot_username is None or self._bot_username_token != token:
                result = _bot_api_call(token, "getMe", timeout=10)
                if result.get("ok"):
                    self._bot_username = (result.get("result") or {}).get("username")
                    self._bot_username_token = token

            poll_timeout = _env_float("TELEGRAM_COMMANDS_POLL_TIMEOUT_SEC", 25, 1)
            params = {
                "timeout": str(int(poll_timeout)),
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
            if self._last_update_id is not None:
                params["offset"] = str(self._last_update_id + 1)

            result = _bot_api_call(token, "getUpdates", params, timeout=poll_timeout + 5)
            if not result.get("ok"):
                failures += 1
                # Exponential backoff (5 s .. 60 s): during an internet outage
                # the old fixed 5 s retry wrote thousands of identical warnings.
                delay = min(60.0, 5.0 * (2 ** min(failures - 1, 4)))
                logger.warning(
                    "Polling comandi Telegram fallito: %s",
                    result.get("description") or "errore sconosciuto",
                )
                self._stop.wait(delay)
                continue
            if failures:
                logger.info("Polling comandi Telegram ripristinato dopo %d errori", failures)
                failures = 0
            # After a reboot the service often starts before the network: the
            # first setMyCommands fails, so retry once Telegram is reachable.
            self._set_commands_menu()

            for update in result.get("result", []):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._last_update_id = update_id
                self._submit_update(update)

    def _submit_update(self, update: dict) -> None:
        if "callback_query" in update:
            chat = ((update["callback_query"].get("message") or {}).get("chat")) or {}
            handler = self._handle_callback
            payload = update["callback_query"]
        else:
            chat = (update.get("message") or {}).get("chat") or {}
            handler = self._handle_update
            payload = update
        key = str(chat.get("id", "")) or "unknown"
        executor = self._executor
        if executor is None:
            handler(payload)
            return
        if not executor.submit(key, lambda: handler(payload)):
            logger.warning("Coda comandi piena per la chat %s: update scartato", key)

    def _forget_pending_updates(self) -> None:
        token = self._token()
        if not token:
            return
        result = _bot_api_call(
            token,
            "getUpdates",
            {
                "offset": "-1",
                "limit": "1",
                "timeout": "0",
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=5,
        )
        if not result.get("ok"):
            return
        for update in result.get("result", []):
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._last_update_id = update_id

    def _set_commands_menu(self) -> None:
        if self._menu_configured or not _env_bool("TELEGRAM_COMMANDS_SET_MENU", True):
            return
        token = self._token()
        if not token:
            return
        payload = {
            "commands": json.dumps(
                [
                    {"command": command, "description": description}
                    for command, description in COMMANDS
                ]
            )
        }
        result = _bot_api_call(token, "setMyCommands", payload)
        if not result.get("ok"):
            logger.warning("Menu comandi Telegram non aggiornato: %s", result.get("description"))
            return
        self._menu_configured = True

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        raw_text = str(message.get("text") or "").strip()
        self._ctx.user_id = str((message.get("from") or {}).get("id", "") or "")

        if chat_id not in self._allowed_chat_ids():
            if raw_text.startswith("/start"):
                self._handle_invite(chat_id, message, raw_text)
            return
        if not self._allow_command(chat_id):
            self._send_message(chat_id, "Troppi comandi. Riprova tra poco.")
            return

        # I bottoni della reply keyboard inviano l'etichetta come testo: traducila.
        text = REPLY_BUTTON_COMMANDS.get(raw_text, raw_text)
        command = self._parse_command(text)
        if not command:
            if text:
                self._handle_free_text(chat_id, text)
            return
        args = text.split()[1:]
        response = self._dispatch(command, args, chat_id)
        if response:
            self._send_message(chat_id, response)

    def _handle_free_text(self, chat_id: str, text: str) -> None:
        """Messaggio senza `/comando`: prova a interpretarlo con l'agente NLU.

        Se il layer agentico non e' abilitato, ignora silenziosamente (stesso
        comportamento di prima: solo i `/comandi` espliciti erano gestiti).
        """
        agent = getattr(self.services, "agent", None)
        if agent is None:
            return
        proposal = agent.propose(text, "telegram", chat_id)
        if not proposal.ok:
            self._send_message(chat_id, proposal.error or "Non ho capito, usa /help.")
            return
        if not self._is_admin(chat_id) and not proposal.readonly:
            if proposal.pending_id:
                agent.cancel(proposal.pending_id, "telegram", chat_id)
            self._send_message(chat_id, "Comando riservato agli amministratori.")
            return
        if proposal.executed:
            # La risposta naturale composta dall'LLM (solo domande readonly)
            # sostituisce l'output grezzo; foto/video passano da _send_result.
            if proposal.answer:
                self._send_message(chat_id, f"🤖 {proposal.answer}")
                return
            response = self._send_result(chat_id, proposal.result) if proposal.result else None
            if response:
                self._send_message(chat_id, f"🤖 {response}")
            return
        markup = json.dumps(
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ Conferma",
                            "callback_data": f"/agent_confirm {proposal.pending_id}",
                        },
                        {
                            "text": "❌ Annulla",
                            "callback_data": f"/agent_cancel {proposal.pending_id}",
                        },
                    ]
                ]
            }
        )
        self._send_message(
            chat_id,
            f"🤖 Ho capito: {proposal.description}\nConfermi?",
            reply_markup=markup,
        )

    def _agent_confirm(self, args: list[str], chat_id: str) -> str | None:
        if not self._is_admin(chat_id):
            return "Comando riservato agli amministratori."
        agent = getattr(self.services, "agent", None)
        if agent is None or not args:
            return "Richiesta non valida o scaduta."
        proposal = agent.confirm(args[0], "telegram", chat_id)
        if not proposal.ok:
            return proposal.error or "Richiesta non valida o scaduta."
        audit.record(
            "telegram-agente",
            self._actor(chat_id),
            getattr(proposal, "command", None) or "agent",
            getattr(proposal, "arg", None),
        )
        response = self._send_result(chat_id, proposal.result) if proposal.result else None
        return f"🤖 {response}" if response else None

    def _agent_cancel(self, args: list[str], chat_id: str) -> str | None:
        agent = getattr(self.services, "agent", None)
        if agent is not None and args:
            agent.cancel(args[0], "telegram", chat_id)
        return "Annullato."

    def _handle_callback(self, callback: dict) -> None:
        chat = (callback.get("message") or {}).get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        callback_id = str(callback.get("id", ""))
        self._ctx.user_id = str((callback.get("from") or {}).get("id", "") or "")
        if chat_id not in self._allowed_chat_ids():
            self._answer_callback(callback_id)
            return
        if not self._allow_command(chat_id):
            self._answer_callback(callback_id, "Troppi comandi. Riprova tra poco.")
            return

        text = str(callback.get("data") or "").strip()
        command = self._parse_command(text)
        self._answer_callback(callback_id)
        if not command:
            return
        args = text.split()[1:]
        response = self._dispatch(command, args, chat_id)
        if response:
            self._send_message(chat_id, response)

    def _legacy_invite_code(self) -> str:
        # The static TELEGRAM_INVITE_CODE never expires and can be reused: it is
        # ignored unless explicitly re-enabled. /invite mints one-time codes.
        if not _env_bool("TELEGRAM_INVITE_LEGACY", False):
            return ""
        return _env("TELEGRAM_INVITE_CODE")

    def _handle_invite(self, chat_id: str, message: dict, text: str) -> None:
        parts = text.split(maxsplit=1)
        provided = parts[1].strip() if len(parts) > 1 else ""
        legacy = self._legacy_invite_code()
        if not legacy and telegram_invites.pending_count() == 0:
            return  # no invite open: stay silent towards unknown chats
        role = telegram_invites.redeem_invite(provided) if provided else None
        if role is None and legacy and provided and hmac.compare_digest(provided, legacy):
            role = telegram_invites.ROLE_FAMILY
        if role is None:
            self._send_message(
                chat_id, "Codice non valido o scaduto. Chiedi un nuovo invito a un amministratore."
            )
            audit.record("telegram", f"sconosciuto:{chat_id}", "invite_rejected", ok=False)
            return
        sender = message.get("from") or {}
        name = (
            " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
            or sender.get("username")
            or chat_id
        )
        with self._guests_lock:
            guests = self._load_guests()
            guests[chat_id] = {
                "name": name,
                "role": role,
                "joined_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._save_guests(guests)
        logger.info("Nuovo ospite Telegram autorizzato: %s (%s, %s)", name, chat_id, role)
        audit.record("telegram", f"{name}:{chat_id}", "invite_accepted", role)
        self._send_message(
            chat_id,
            f"Benvenuto, {name}! Sei autorizzato ({role}). Usa /help per i comandi.",
            reply_markup=_reply_keyboard_markup(),
        )

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        if not callback_id:
            return
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        _bot_api_call(self._token(), "answerCallbackQuery", params, timeout=10)

    def _allow_command(self, chat_id: str) -> bool:
        limit = _env_int("TELEGRAM_COMMANDS_RATE_LIMIT_PER_MIN", 20, 1)
        now = time.monotonic()
        events = self._rate_events[chat_id]
        while events and now - events[0] > 60:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True

    @staticmethod
    def _parse_command(text: str) -> str | None:
        if not text.startswith("/"):
            return None
        command = text.split(maxsplit=1)[0].lower()
        if "@" in command:
            command = command.split("@", 1)[0]
        return command

    def _dispatch(self, command: str, args: list[str], chat_id: str) -> str | None:
        if command in {"/start", "/help"}:
            self._send_message(
                chat_id, self._help_text(chat_id), reply_markup=_reply_keyboard_markup()
            )
            return None
        if command == "/menu":
            self._send_message(chat_id, "Menu comandi:", reply_markup=_inline_menu_markup())
            return None
        if command == "/clip":
            if not self._is_admin(chat_id):
                return "Comando riservato agli amministratori."
            audit.record("telegram", self._actor(chat_id), "clip", args[0] if args else None)
            return self._send_clip(args, chat_id)
        if command == "/agent_confirm":
            return self._agent_confirm(args, chat_id)
        if command == "/agent_cancel":
            return self._agent_cancel(args, chat_id)
        if command == "/invite":
            return self._invite_info(chat_id, args)
        if command == "/log":
            if not self._is_admin(chat_id):
                return "Comando riservato agli amministratori."
            return audit.format_recent(15)
        if command == "/guests":
            return self._guests_text(chat_id)
        if command == "/revoke":
            return self._revoke_guest(args, chat_id)

        # Tutti gli altri comandi vivono nel command registry condiviso con
        # l'agente (blackframe.commands): un solo posto dove sono definiti
        # nome, descrizione e handler, cosi' Telegram e l'NLU restano in sync.
        name = "ptz_home" if command == "/home" else command[1:]
        spec = COMMAND_REGISTRY.get(name)
        if spec is None or spec.handler is None:
            return "Comando non riconosciuto. Usa /help."
        is_admin = self._is_admin(chat_id)
        if not is_admin and not spec.readonly:
            return "Comando riservato agli amministratori."
        if (
            not is_admin
            and name in telegram_invites.MEDIA_COMMANDS
            and self._guest_role(chat_id) == telegram_invites.ROLE_STATUS
        ):
            return "Il tuo accesso non include immagini della camera."
        arg = args[0] if args else None
        result = registry_execute(name, arg, self.services)
        if not spec.readonly:
            audit.record("telegram", self._actor(chat_id), name, arg)
        return self._send_result(chat_id, result)

    def _send_result(self, chat_id: str, result: CommandResult) -> str | None:
        if result.photo is not None:
            ok, error = self._send_photo_bytes(chat_id, result.photo, result.caption or "")
            return None if ok else f"Invio foto fallito: {error}"
        if result.video is not None:
            ok, error = self._send_video_bytes(chat_id, result.video, result.caption or "")
            return None if ok else f"Invio video fallito: {error}"
        return result.text

    def _help_text(self, chat_id: str | None = None) -> str:
        lines = ["🎥 Comandi BLACKFRAME"]
        for title, commands in HELP_SECTIONS:
            lines.append("")
            lines.append(title)
            for command, description in commands:
                lines.append(f"/{command} - {description}")
        if chat_id and self._is_admin(chat_id):
            lines.append("")
            lines.append("🔑 Admin")
            lines.append("/invite [stato] - Invito monouso (24h); 'stato' = senza immagini")
            lines.append("/guests - Lista ospiti autorizzati")
            lines.append("/revoke <chat_id> - Rimuovi ospite")
            lines.append("/log - Ultime azioni eseguite")
        return "\n".join(lines)

    def _send_clip(self, args: list[str], chat_id: str) -> str | None:
        seconds = CLIP_DEFAULT_SEC
        if args:
            try:
                seconds = int(float(args[0]))
            except ValueError:
                return f"Uso: /clip <secondi> (max {CLIP_MAX_SEC})"
        if seconds < 1:
            return "Indica una durata positiva."
        if seconds > CLIP_MAX_SEC:
            return f"Durata massima {CLIP_MAX_SEC} secondi."
        if self.services.camera.get_raw_frame() is None:
            return "Nessun frame disponibile."
        if not self._reserve_clip():
            return "Una clip e gia in registrazione. Riprova tra poco."
        threading.Thread(
            target=self._record_and_send_clip,
            args=(chat_id, seconds, True),
            daemon=True,
        ).start()
        return f"Registro clip di {seconds}s, attendi..."

    def _reserve_clip(self) -> bool:
        maximum = _env_int("TELEGRAM_CLIP_MAX_CONCURRENT", 1, 1)
        with self._clip_lock:
            if self._active_clips >= maximum:
                return False
            self._active_clips += 1
            return True

    def _record_and_send_clip(self, chat_id: str, seconds: int, reserved: bool = False) -> None:
        if not reserved and not self._reserve_clip():
            self._send_message(chat_id, "Una clip e gia in registrazione. Riprova tra poco.")
            return
        fps = _env_float("RECORD_FPS", 10, 1)
        max_width = _env_int("RECORD_MAX_WIDTH", 1280, 0)
        tmp_dir = tempfile.mkdtemp(prefix="blackframe_clip_")
        path = Path(tmp_dir) / "clip.mp4"
        try:
            result = record_clip(self.services.camera, path, seconds, fps=fps, max_width=max_width)
            if result is None:
                self._send_message(chat_id, "Registrazione clip fallita.")
                return
            video = result.read_bytes()
            ok, error = self._send_video_bytes(chat_id, video, f"Clip live {seconds}s")
            if not ok:
                self._send_message(chat_id, f"Invio clip fallito: {error}")
        except Exception:
            logger.exception("Clip on-demand fallita")
            self._send_message(chat_id, "Errore durante la clip.")
        finally:
            try:
                path.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except OSError:
                pass
            with self._clip_lock:
                self._active_clips = max(0, self._active_clips - 1)

    def _invite_info(self, chat_id: str, args: list[str] | None = None) -> str:
        if not self._is_admin(chat_id):
            return "Comando riservato agli amministratori."
        role = telegram_invites.ROLE_FAMILY
        if args and args[0].strip().lower() in telegram_invites.ROLES:
            role = args[0].strip().lower()
        code, expires_at = telegram_invites.create_invite(role, created_by=chat_id)
        audit.record("telegram", self._actor(chat_id), "invite_created", role)
        expiry = time.strftime("%d/%m %H:%M", time.localtime(expires_at))
        lines = [
            f"Invito monouso ({role}), valido fino al {expiry}:",
            f"Codice: {code}",
        ]
        if self._bot_username:
            lines.append(f"Link: https://t.me/{self._bot_username}?start={code}")
        if role == telegram_invites.ROLE_FAMILY:
            lines.append("Accesso: comandi di sola lettura, incluse foto della camera.")
        else:
            lines.append("Accesso: solo stato ed elenco eventi, nessuna immagine.")
        return "\n".join(lines)

    def _guests_text(self, chat_id: str) -> str:
        if not self._is_admin(chat_id):
            return "Comando riservato agli amministratori."
        with self._guests_lock:
            guests = self._load_guests()
        if not guests:
            return "Nessun ospite autorizzato."
        lines = ["Ospiti autorizzati:"]
        for gid, info in guests.items():
            name = info.get("name") or gid
            joined = info.get("joined_at") or "?"
            role = info.get("role") or telegram_invites.ROLE_FAMILY
            lines.append(f"- {name} ({gid}) — {role} — {joined}")
        return "\n".join(lines)

    def _revoke_guest(self, args: list[str], chat_id: str) -> str:
        if not self._is_admin(chat_id):
            return "Comando riservato agli amministratori."
        if not args:
            return "Uso: /revoke <chat_id>"
        target_id = args[0].strip()
        with self._guests_lock:
            guests = self._load_guests()
            if target_id not in guests:
                return f"Ospite {target_id} non trovato."
            name = guests.pop(target_id).get("name") or target_id
            self._save_guests(guests)
        logger.info("Ospite Telegram rimosso: %s (%s)", name, target_id)
        audit.record("telegram", self._actor(chat_id), "revoke", target_id)
        return f"Ospite {name} ({target_id}) rimosso."

    def _send_message(
        self, chat_id: str, text: str, reply_markup: str | None = None
    ) -> tuple[bool, str | None]:
        result = telegram_client.send_message(self._token(), chat_id, text, reply_markup)
        if result.get("ok"):
            return True, None
        return False, result.get("description") or "Invio fallito"

    def _send_photo_bytes(
        self,
        chat_id: str,
        photo: bytes,
        caption: str,
    ) -> tuple[bool, str | None]:
        result = telegram_client.send_photo(self._token(), chat_id, data=photo, caption=caption)
        if result.get("ok"):
            return True, None
        return False, result.get("description") or "Invio fallito"

    def _send_video_bytes(
        self,
        chat_id: str,
        video: bytes,
        caption: str,
    ) -> tuple[bool, str | None]:
        result = telegram_client.send_video(
            self._token(), chat_id, data=video, caption=caption, timeout=60
        )
        if result.get("ok"):
            return True, None
        return False, result.get("description") or "Invio fallito"
