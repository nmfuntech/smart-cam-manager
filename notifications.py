"""Telegram push notifications for motion events.

Configuration is read from the environment on every call so that runtime config
updates take effect without a restart. Network I/O runs in a background thread to
avoid blocking the motion detection loop.
"""

import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


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

    @property
    def enabled(self) -> bool:
        return _env_bool("NOTIFY_TELEGRAM_ENABLED", False)

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
        if not self.enabled:
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

    def _caption(self, event_id: str, class_label: str | None) -> str:
        label = event_id.replace("motion_event_", "")
        if class_label:
            return f"🚨 Movimento rilevato ({class_label}) — {label}"
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
        if not self.enabled:
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
