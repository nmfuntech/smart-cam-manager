"""Telegram command polling for BLACKFRAME.

The command bot uses Telegram long polling, so the app does not need a public
webhook URL. It is disabled by default and only accepts exact configured chat IDs.
"""

from __future__ import annotations

import json
import logging
import os
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

from notifications import TELEGRAM_API_BASE, telegram_api_call
from recording import record_clip

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

PTZ_COMMANDS = {
    "/ptz_left": "left",
    "/ptz_right": "right",
    "/ptz_up": "up",
    "/ptz_down": "down",
}

# Soglia movimento: valore piu basso = piu sensibile. Accetta preset IT ed EN.
SENSITIVITY_PRESETS = {
    "alta": 15,
    "media": 30,
    "bassa": 50,
    "high": 15,
    "medium": 30,
    "low": 50,
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(_env(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


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

    @property
    def enabled(self) -> bool:
        return _env_bool("TELEGRAM_COMMANDS_ENABLED", False)

    def _token(self) -> str:
        return _env("NOTIFY_TELEGRAM_BOT_TOKEN")

    def _allowed_chat_ids(self) -> set[str]:
        raw = _env("TELEGRAM_COMMANDS_ALLOWED_CHAT_IDS") or _env("NOTIFY_TELEGRAM_CHAT_ID")
        return {item.strip() for item in raw.split(",") if item.strip()}

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
        return {
            "enabled": self.enabled,
            "configured": self.configured(),
            "running": running,
            "allowed_chat_count": len(self._allowed_chat_ids()),
        }

    def _run(self) -> None:
        self._forget_pending_updates()
        self._set_commands_menu()
        while not self._stop.is_set():
            token = self._token()
            if not self.enabled or not token or not self._allowed_chat_ids():
                time.sleep(5)
                continue

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
        if chat_id not in self._allowed_chat_ids():
            return
        if not self._allow_command(chat_id):
            self._send_message(chat_id, "Troppi comandi. Riprova tra poco.")
            return

        text = str(message.get("text") or "").strip()
        # I bottoni della reply keyboard inviano l'etichetta come testo: traducila.
        text = REPLY_BUTTON_COMMANDS.get(text, text)
        command = self._parse_command(text)
        if not command:
            return
        args = text.split()[1:]
        response = self._dispatch(command, args, chat_id)
        if response:
            self._send_message(chat_id, response)

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
            self._send_message(chat_id, self._help_text(), reply_markup=_reply_keyboard_markup())
            return None
        if command == "/menu":
            self._send_message(chat_id, "Menu comandi:", reply_markup=_inline_menu_markup())
            return None
        if command == "/status":
            return self._status_text()
        if command == "/config":
            return self._config_text()
        if command == "/snapshot":
            return self._send_snapshot(chat_id)
        if command == "/clip":
            return self._send_clip(args, chat_id)
        if command == "/latest":
            return self._send_latest_event(chat_id)
        if command == "/events":
            return self._events_text()
        if command == "/motion_on":
            return self._update_bool("MOTION_ENABLED", True, "Rilevamento movimento attivato.")
        if command == "/motion_off":
            return self._update_bool("MOTION_ENABLED", False, "Rilevamento movimento disattivato.")
        if command == "/sensitivity":
            return self._set_sensitivity(args)
        if command == "/notifications_on":
            return self._update_bool("NOTIFY_TELEGRAM_ENABLED", True, "Notifiche attivate.")
        if command == "/notifications_off":
            return self._update_bool("NOTIFY_TELEGRAM_ENABLED", False, "Notifiche disattivate.")
        if command == "/mute":
            return self._mute(args)
        if command == "/resume":
            return self._resume()
        if command == "/classification_on":
            return self._update_bool("CLASSIFICATION_ENABLED", True, "Riconoscimento attivato.")
        if command == "/classification_off":
            return self._update_bool("CLASSIFICATION_ENABLED", False, "Riconoscimento disattivato.")
        if command == "/record_on":
            return self._update_bool("RECORD_ENABLED", True, "Clip video evento attivate.")
        if command == "/record_off":
            return self._update_bool("RECORD_ENABLED", False, "Clip video evento disattivate.")
        if command == "/continuous_on":
            return self._update_bool(
                "CONTINUOUS_RECORD_ENABLED",
                True,
                "Registrazione continua attivata.",
            )
        if command == "/continuous_off":
            return self._update_bool(
                "CONTINUOUS_RECORD_ENABLED",
                False,
                "Registrazione continua disattivata.",
            )
        if command in PTZ_COMMANDS:
            return self._ptz_move(PTZ_COMMANDS[command])
        if command == "/ptz_stop":
            return self._ptz_stop()
        if command in {"/ptz_home", "/home"}:
            return self._ptz_home()
        return "Comando non riconosciuto. Usa /help."

    def _help_text(self) -> str:
        lines = ["🎥 Comandi BLACKFRAME"]
        for title, commands in HELP_SECTIONS:
            lines.append("")
            lines.append(title)
            for command, description in commands:
                lines.append(f"/{command} - {description}")
        return "\n".join(lines)

    def _status_text(self) -> str:
        stream = self.services.camera.get_status()
        motion = self.services.motion.get_status()
        ptz = self.services.ptz.get_status()
        continuous = (
            self.services.continuous.status()
            if getattr(self.services, "continuous", None) is not None
            else {"enabled": False, "active": False}
        )
        stream_state = stream.get("connection_state") or (
            "online" if stream.get("connected") else "offline"
        )
        motion_state = "attivo" if motion.get("enabled") else "spento"
        moving = "si" if motion.get("motion_detected") else "no"
        ptz_state = (
            "ok" if ptz.get("available") else f"no ({ptz.get('error') or 'non disponibile'})"
        )
        cont_state = "attiva" if continuous.get("active") else "spenta"
        return "\n".join(
            [
                "Stato BLACKFRAME",
                f"Stream: {stream_state}",
                f"Motion: {motion_state}, movimento: {moving}",
                f"Notifiche: {self._notifications_state()}",
                f"Ultimo evento: {motion.get('last_motion_at') or '-'}",
                f"PTZ: {ptz_state}",
                f"Registrazione continua: {cont_state}",
            ]
        )

    def _notifier(self) -> Any | None:
        features = getattr(self.services, "features", None)
        return getattr(features, "telegram", None) if features is not None else None

    def _notifications_state(self) -> str:
        if not _env_bool("NOTIFY_TELEGRAM_ENABLED", False):
            return "spente"
        notifier = self._notifier()
        remaining = notifier.muted_remaining() if notifier is not None else 0
        if remaining > 0:
            return f"in pausa ({int(remaining // 60) + 1} min)"
        return "attive"

    def _config_text(self) -> str:
        def flag(name: str, on: str, off: str, default: bool = False) -> str:
            return on if _env_bool(name, default) else off

        return "\n".join(
            [
                "Impostazioni BLACKFRAME",
                f"Movimento: {flag('MOTION_ENABLED', 'attivo', 'spento', True)}",
                f"Soglia movimento: {_env_int('MOTION_THRESHOLD', 30, 0)}",
                f"Notifiche: {self._notifications_state()}",
                f"Riconoscimento: {flag('CLASSIFICATION_ENABLED', 'attivo', 'spento')}",
                f"Clip evento: {flag('RECORD_ENABLED', 'attive', 'spente')}",
                f"Reg. continua: {flag('CONTINUOUS_RECORD_ENABLED', 'attiva', 'spenta')}",
            ]
        )

    def _set_sensitivity(self, args: list[str]) -> str:
        if not args:
            return "Uso: /sensitivity bassa|media|alta"
        preset = args[0].lower()
        threshold = SENSITIVITY_PRESETS.get(preset)
        if threshold is None:
            return "Preset sconosciuto. Usa: bassa, media o alta."
        try:
            self._apply_runtime_updates({"MOTION_THRESHOLD": threshold})
        except ValueError as exc:
            return f"Config non valida: {exc}"
        except Exception:
            logger.exception("Aggiornamento sensibilita da Telegram fallito")
            return "Aggiornamento fallito."
        return f"Sensibilita impostata su {preset} (soglia {threshold})."

    def _mute(self, args: list[str]) -> str:
        notifier = self._notifier()
        if notifier is None:
            return "Notifiche non disponibili."
        minutes = 15.0
        if args:
            try:
                minutes = float(args[0])
            except ValueError:
                return "Uso: /mute <minuti>"
        if minutes <= 0:
            return "Indica un numero di minuti positivo."
        notifier.mute(minutes * 60)
        return f"Notifiche silenziate per {int(minutes)} min."

    def _resume(self) -> str:
        notifier = self._notifier()
        if notifier is None:
            return "Notifiche non disponibili."
        notifier.mute(0)
        return "Notifiche riprese."

    def _events_text(self) -> str:
        events = self.services.motion.list_events(limit=5)
        if not events:
            return "Nessun evento salvato."
        lines = ["Ultimi eventi:"]
        for event in events:
            label = event.get("label") or event.get("id")
            frames = event.get("frame_count")
            classification = (event.get("classification") or {}).get("class_label")
            suffix = f" ({classification})" if classification else ""
            frame_text = f", {frames} frame" if frames is not None else ""
            lines.append(f"- {label}{suffix}{frame_text}")
        return "\n".join(lines)

    def _send_snapshot(self, chat_id: str) -> str | None:
        frame = self.services.camera.get_frame()
        if frame is None:
            return "Nessun frame disponibile."
        ok, error = self._send_photo_bytes(chat_id, frame, "Snapshot live BLACKFRAME")
        if not ok:
            return f"Invio snapshot fallito: {error}"
        return None

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

    def _send_latest_event(self, chat_id: str) -> str | None:
        events = self.services.motion.list_events(limit=1)
        if not events:
            return "Nessun evento salvato."
        event = events[0]
        preview = Path(str(event.get("preview_path") or ""))
        if not preview.is_file():
            return "Anteprima ultimo evento non disponibile."
        try:
            photo = preview.read_bytes()
        except OSError:
            return "Anteprima ultimo evento non leggibile."
        caption = f"Ultimo evento: {event.get('label') or event.get('id')}"
        ok, error = self._send_photo_bytes(chat_id, photo, caption)
        if not ok:
            return f"Invio ultimo evento fallito: {error}"
        return None

    def _update_bool(self, key: str, value: bool, success: str) -> str:
        try:
            self._apply_runtime_updates({key: value})
        except ValueError as exc:
            return f"Config non valida: {exc}"
        except Exception:
            logger.exception("Aggiornamento runtime da Telegram fallito")
            return "Aggiornamento fallito."
        return success

    def _apply_runtime_updates(self, updates: dict[str, object]) -> None:
        self.services.runtime_config.update(updates)
        self.services.camera.apply_runtime_config(updates)
        self.services.ptz.apply_runtime_config(updates)
        self.services.motion.apply_runtime_config(updates)
        if "CONTINUOUS_RECORD_ENABLED" in updates and getattr(self.services, "continuous", None):
            self.services.continuous.apply_config(self.services.motion.config)

    def _ptz_move(self, direction: str) -> str:
        success, error = self.services.ptz.move(direction)
        return "PTZ mosso." if success else f"PTZ fallito: {error}"

    def _ptz_stop(self) -> str:
        success, error = self.services.ptz.stop()
        return "PTZ fermato." if success else f"Stop PTZ fallito: {error}"

    def _ptz_home(self) -> str:
        success, error = self.services.ptz.home()
        return "PTZ riportato home." if success else f"PTZ home fallito: {error}"

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
