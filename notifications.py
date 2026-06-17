"""Telegram push notifications for motion events.

Configuration is read from the environment on every call so that runtime config
updates take effect without a restart. Network I/O runs in a background thread to
avoid blocking the motion detection loop.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def telegram_api_call(token: str, method: str, params: dict | None = None) -> dict:
    """Call a Telegram Bot API method. Returns the parsed JSON response.

    On network/HTTP failure returns a dict shaped like the Telegram error
    response: ``{"ok": False, "description": "..."}``.
    """
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def discover_telegram_chats(token: str) -> tuple[bool, list[dict], str | None]:
    """Find chats that recently messaged the bot via getUpdates.

    Returns ``(ok, chats, error)`` where ``chats`` is a list of
    ``{"chat_id": int, "label": str}`` dicts.
    """
    result = telegram_api_call(token, "getUpdates")
    if not result.get("ok"):
        return False, [], result.get("description") or "Token non valido"
    chats: dict[int, str] = {}
    for upd in result.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat and "id" in chat:
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            chats[chat["id"]] = f"{name} ({chat.get('type')})"
    return True, [{"chat_id": cid, "label": label} for cid, label in chats.items()], None


def send_telegram_test(
    token: str, chat_id: str, text: str | None = None
) -> tuple[bool, str | None]:
    """Send a plain-text test message. Returns ``(ok, error)``."""
    message = text or "✅ BLACKFRAME: notifiche Telegram configurate correttamente."
    result = telegram_api_call(token, "sendMessage", {"chat_id": chat_id, "text": message})
    if result.get("ok"):
        return True, None
    return False, result.get("description") or "Invio fallito"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_multipart(
    fields: dict[str, str],
    file_field: str | None,
    file_path: str | None,
    file_content_type: str = "image/jpeg",
):
    """Encode a multipart/form-data body. Returns (content_type, body_bytes)."""
    boundary = f"----blackframe{uuid.uuid4().hex}"
    crlf = b"\r\n"
    body = bytearray()
    for name, value in fields.items():
        body += b"--" + boundary.encode() + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    if file_field and file_path:
        filename = Path(file_path).name
        with open(file_path, "rb") as fh:
            file_bytes = fh.read()
        body += b"--" + boundary.encode() + crlf
        body += (
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'
        ).encode() + crlf
        body += f"Content-Type: {file_content_type}".encode() + crlf + crlf
        body += file_bytes + crlf
    body += b"--" + boundary.encode() + b"--" + crlf
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, bytes(body)


class TelegramNotifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_sent_at: float | None = None
        self._muted_until: float | None = None

    @property
    def enabled(self) -> bool:
        return _env_bool("NOTIFY_TELEGRAM_ENABLED", False)

    def mute(self, seconds: float) -> float:
        """Silenzia temporaneamente le notifiche. seconds<=0 annulla la pausa.

        Ritorna i secondi residui di pausa (0 se non in pausa).
        """
        with self._lock:
            if seconds > 0:
                self._muted_until = time.monotonic() + seconds
            else:
                self._muted_until = None
        return self.muted_remaining()

    def muted_remaining(self) -> float:
        with self._lock:
            if self._muted_until is None:
                return 0.0
            remaining = self._muted_until - time.monotonic()
            if remaining <= 0:
                self._muted_until = None
                return 0.0
            return remaining

    def _is_muted(self) -> bool:
        return self.muted_remaining() > 0

    def _bot_token(self) -> str:
        return _env("NOTIFY_TELEGRAM_BOT_TOKEN")

    def _chat_id(self) -> str:
        return _env("NOTIFY_TELEGRAM_CHAT_ID")

    def _allowed_classes(self) -> set[str]:
        raw = _env("NOTIFY_ON_CLASSES")
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _min_interval(self) -> float:
        try:
            return max(0.0, float(_env("NOTIFY_MIN_INTERVAL_SEC", "30")))
        except ValueError:
            return 30.0

    def notify_event(
        self,
        event_id: str,
        class_label: str | None = None,
        image_path: str | None = None,
    ) -> bool:
        """Queue a Telegram notification for an event. Returns True if accepted for sending."""
        if not self.enabled or self._is_muted():
            return False
        token = self._bot_token()
        chat_id = self._chat_id()
        if not token or not chat_id:
            logger.warning("Telegram non configurato: token o chat_id mancanti")
            return False

        allowed = self._allowed_classes()
        if allowed and (class_label is None or class_label not in allowed):
            return False

        with self._lock:
            now = time.monotonic()
            if self._last_sent_at is not None and now - self._last_sent_at < self._min_interval():
                return False
            self._last_sent_at = now

        threading.Thread(
            target=self._send,
            args=(token, chat_id, event_id, class_label, image_path),
            daemon=True,
        ).start()
        return True

    # Emoji per categoria classificata, mostrata accanto all'etichetta.
    _CLASS_EMOJI = {"persona": "🧍", "animale_domestico": "🐕"}

    def _caption(self, event_id: str, class_label: str | None) -> str:
        label = event_id.replace("motion_event_", "")
        if class_label:
            emoji = self._CLASS_EMOJI.get(class_label)
            prefix = f"{emoji} " if emoji else ""
            return f"🚨 Movimento rilevato — {prefix}{class_label} ({label})"
        return f"🚨 Movimento rilevato — {label}"

    def _send(
        self,
        token: str,
        chat_id: str,
        event_id: str,
        class_label: str | None,
        image_path: str | None,
    ) -> None:
        caption = self._caption(event_id, class_label)
        try:
            if image_path and Path(image_path).is_file():
                url = f"{TELEGRAM_API_BASE}/bot{token}/sendPhoto"
                content_type, body = _build_multipart(
                    {"chat_id": chat_id, "caption": caption},
                    "photo",
                    image_path,
                )
            else:
                url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
                content_type, body = _build_multipart(
                    {"chat_id": chat_id, "text": caption}, None, None
                )
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", content_type)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Invio Telegram fallito: %s", exc)
        except Exception:
            logger.exception("Errore inatteso invio Telegram")

    def notify_event_video(
        self,
        event_id: str,
        class_label: str | None = None,
        video_path: str | None = None,
    ) -> bool:
        """Queue a Telegram sendVideo notification. Returns True if accepted for sending."""
        if not self.enabled or self._is_muted():
            return False
        token = self._bot_token()
        chat_id = self._chat_id()
        if not token or not chat_id:
            logger.warning("Telegram non configurato: token o chat_id mancanti")
            return False

        allowed = self._allowed_classes()
        if allowed and (class_label is None or class_label not in allowed):
            return False

        with self._lock:
            now = time.monotonic()
            if self._last_sent_at is not None and now - self._last_sent_at < self._min_interval():
                return False
            self._last_sent_at = now

        threading.Thread(
            target=self._send_video,
            args=(token, chat_id, event_id, class_label, video_path),
            daemon=True,
        ).start()
        return True

    def _send_video(
        self,
        token: str,
        chat_id: str,
        event_id: str,
        class_label: str | None,
        video_path: str | None,
    ) -> None:
        caption = self._caption(event_id, class_label)
        try:
            if video_path and Path(video_path).is_file():
                url = f"{TELEGRAM_API_BASE}/bot{token}/sendVideo"
                content_type, body = _build_multipart(
                    {"chat_id": chat_id, "caption": caption, "supports_streaming": "true"},
                    "video",
                    video_path,
                    file_content_type="video/mp4",
                )
            else:
                url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
                content_type, body = _build_multipart(
                    {"chat_id": chat_id, "text": caption}, None, None
                )
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", content_type)
            with urllib.request.urlopen(req, timeout=60) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Invio video Telegram fallito: %s", exc)
        except Exception:
            logger.exception("Errore inatteso invio video Telegram")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": bool(self._bot_token() and self._chat_id()),
            "classes": sorted(self._allowed_classes()),
            "min_interval_sec": self._min_interval(),
        }
