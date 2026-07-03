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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from blackframe.commands import COMMAND_REGISTRY, CommandResult
from blackframe.commands import execute as registry_execute
from blackframe.envutil import env_bool as _env_bool
from blackframe.envutil import env_float, env_int
from blackframe.envutil import env_str as _env
from blackframe.notifications import TELEGRAM_API_BASE, telegram_api_call
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
    ["🎬 Clip", "📋 Menu"],
]
REPLY_BUTTON_COMMANDS = {
    "📊 Stato": "/status",
    "📸 Snapshot": "/snapshot",
    "🎬 Clip": "/clip",
    "📋 Menu": "/menu",
}

# Inline keyboard del /menu: tutto il resto. callback_data = comando (+ args).
INLINE_MENU_ROWS = [
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


def _build_multipart_bytes(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    file_content_type: str,
) -> tuple[str, bytes]:
    boundary = f"----blackframe{uuid.uuid4().hex}"
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + boundary.encode() + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    body += b"--" + boundary.encode() + crlf
    body += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'
    ).encode() + crlf
    body += f"Content-Type: {file_content_type}".encode() + crlf + crlf
    body += file_bytes + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def _bot_api_call(
    token: str,
    method: str,
    params: dict | None = None,
    timeout: float = 15,
) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {exc.code}: {body}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": f"Errore di rete: {exc}"}
    except json.JSONDecodeError:
        return {"ok": False, "description": "Risposta Telegram non valida"}


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
        return chat_id in self._admin_chat_ids()

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
                logger.warning(
                    "Polling comandi Telegram fallito: %s",
                    result.get("description") or "errore sconosciuto",
                )
                time.sleep(5)
                continue

            for update in result.get("result", []):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._last_update_id = update_id
                try:
                    if "callback_query" in update:
                        self._handle_callback(update["callback_query"])
                    else:
                        self._handle_update(update)
                except Exception:
                    logger.exception("Gestione comando Telegram fallita")

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

        if chat_id not in self._allowed_chat_ids():
            invite_code = _env("TELEGRAM_INVITE_CODE")
            if raw_text.startswith("/start") and invite_code:
                self._handle_invite(chat_id, message, raw_text, invite_code)
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
        agent = getattr(self.services, "agent", None)
        if agent is None or not args:
            return "Richiesta non valida o scaduta."
        proposal = agent.confirm(args[0], "telegram", chat_id)
        if not proposal.ok:
            return proposal.error or "Richiesta non valida o scaduta."
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

    def _handle_invite(self, chat_id: str, message: dict, text: str, invite_code: str) -> None:
        parts = text.split(maxsplit=1)
        provided = parts[1].strip() if len(parts) > 1 else ""
        if not provided or not hmac.compare_digest(provided, invite_code):
            self._send_message(
                chat_id, "Codice non valido. Chiedi il link di invito a un amministratore."
            )
            return
        sender = message.get("from") or {}
        name = (
            " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
            or sender.get("username")
            or chat_id
        )
        with self._guests_lock:
            guests = self._load_guests()
            guests[chat_id] = {"name": name, "joined_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self._save_guests(guests)
        logger.info("Nuovo ospite Telegram autorizzato: %s (%s)", name, chat_id)
        self._send_message(
            chat_id,
            f"Benvenuto, {name}! Sei autorizzato. Usa /help per i comandi.",
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
            return self._send_clip(args, chat_id)
        if command == "/agent_confirm":
            return self._agent_confirm(args, chat_id)
        if command == "/agent_cancel":
            return self._agent_cancel(args, chat_id)
        if command == "/invite":
            return self._invite_info(chat_id)
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
        arg = args[0] if args else None
        result = registry_execute(name, arg, self.services)
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
            lines.append("/invite - Link di invito familiare")
            lines.append("/guests - Lista ospiti autorizzati")
            lines.append("/revoke <chat_id> - Rimuovi ospite")
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
        threading.Thread(
            target=self._record_and_send_clip,
            args=(chat_id, seconds),
            daemon=True,
        ).start()
        return f"Registro clip di {seconds}s, attendi..."

    def _record_and_send_clip(self, chat_id: str, seconds: int) -> None:
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

    def _invite_info(self, chat_id: str) -> str:
        if not self._is_admin(chat_id):
            return "Comando riservato agli amministratori."
        code = _env("TELEGRAM_INVITE_CODE")
        if not code:
            return "Inviti disabilitati (TELEGRAM_INVITE_CODE non impostato)."
        lines = [f"Codice di invito: {code}"]
        if self._bot_username:
            lines.append(f"Link: https://t.me/{self._bot_username}?start={code}")
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
            lines.append(f"- {name} ({gid}) — {joined}")
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
        return f"Ospite {name} ({target_id}) rimosso."

    def _send_message(
        self, chat_id: str, text: str, reply_markup: str | None = None
    ) -> tuple[bool, str | None]:
        params = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        result = telegram_api_call(self._token(), "sendMessage", params)
        if result.get("ok"):
            return True, None
        return False, result.get("description") or "Invio fallito"

    def _send_photo_bytes(
        self,
        chat_id: str,
        photo: bytes,
        caption: str,
    ) -> tuple[bool, str | None]:
        return self._send_media_bytes(
            "sendPhoto", "photo", "blackframe.jpg", "image/jpeg", chat_id, photo, caption
        )

    def _send_video_bytes(
        self,
        chat_id: str,
        video: bytes,
        caption: str,
    ) -> tuple[bool, str | None]:
        return self._send_media_bytes(
            "sendVideo", "video", "clip.mp4", "video/mp4", chat_id, video, caption, timeout=60
        )

    def _send_media_bytes(
        self,
        method: str,
        field: str,
        filename: str,
        media_content_type: str,
        chat_id: str,
        data: bytes,
        caption: str,
        timeout: float = 20,
    ) -> tuple[bool, str | None]:
        url = f"{TELEGRAM_API_BASE}/bot{self._token()}/{method}"
        content_type, body = _build_multipart_bytes(
            {"chat_id": chat_id, "caption": caption},
            field,
            filename,
            data,
            media_content_type,
        )
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body_text)
            except json.JSONDecodeError:
                return False, f"HTTP {exc.code}: {body_text}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"Errore di rete: {exc}"
        except json.JSONDecodeError:
            return False, "Risposta Telegram non valida"

        if payload.get("ok"):
            return True, None
        return False, payload.get("description") or "Invio fallito"
