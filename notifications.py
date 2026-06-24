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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
# Limite prudente per sendVideo (Telegram accetta ~50MB; oltre ~20MB spesso fallisce).
DEFAULT_MAX_VIDEO_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class _DeliveryJob:
    event_id: str
    class_label: str | None
    image_path: str | None
    video_path: str | None
    token: str
    chat_id: str
    on_delivered: Callable[[], None] | None = None


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
        self._pending: list[_DeliveryJob] = []
        self._worker_running = False

    def _max_video_bytes(self) -> int:
        try:
            mb = float(_env("NOTIFY_TELEGRAM_MAX_VIDEO_MB", "20"))
        except ValueError:
            mb = 20.0
        return max(1, int(mb * 1024 * 1024))

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker_running:
                return
            self._worker_running = True
            threading.Thread(target=self._delivery_worker, daemon=True).start()

    def _enqueue(self, job: _DeliveryJob) -> bool:
        self._start_worker()
        with self._lock:
            self._pending.append(job)
        return True

    def _delivery_worker(self) -> None:
        while True:
            if self._is_muted():
                time.sleep(1.0)
                continue
            with self._lock:
                job = self._pending.pop(0) if self._pending else None
            if job is None:
                time.sleep(0.25)
                continue
            with self._lock:
                if self._last_sent_at is not None:
                    wait = self._min_interval() - (time.monotonic() - self._last_sent_at)
                else:
                    wait = 0.0
            if wait > 0:
                time.sleep(wait)
            if self._deliver(job):
                with self._lock:
                    self._last_sent_at = time.monotonic()
                if job.on_delivered is not None:
                    try:
                        job.on_delivered()
                    except Exception:
                        logger.exception("Callback post-invio Telegram fallito")
            else:
                logger.warning("Invio Telegram non riuscito per %s", job.event_id)

    def _deliver(self, job: _DeliveryJob) -> bool:
        if job.video_path and Path(job.video_path).is_file():
            size = Path(job.video_path).stat().st_size
            if size <= self._max_video_bytes():
                if self._send_video(job.token, job.chat_id, job.event_id, job.class_label, job.video_path):
                    return True
            cover = Path(job.video_path).parent / "cover.jpg"
            if cover.is_file():
                logger.info(
                    "Clip %s troppo grande (%d MB), invio foto di copertina su Telegram",
                    job.event_id,
                    size // (1024 * 1024),
                )
                return self._send(job.token, job.chat_id, job.event_id, job.class_label, str(cover))
            logger.warning("Clip %s troppo grande e senza cover.jpg", job.event_id)
            return self._send(job.token, job.chat_id, job.event_id, job.class_label, None)
        return self._send(job.token, job.chat_id, job.event_id, job.class_label, job.image_path)

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
        on_delivered: Callable[[], None] | None = None,
    ) -> bool:
        """Accoda una notifica Telegram. ``on_delivered`` viene chiamato solo dopo invio riuscito."""
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

        return self._enqueue(
            _DeliveryJob(
                event_id=event_id,
                class_label=class_label,
                image_path=image_path,
                video_path=None,
                token=token,
                chat_id=chat_id,
                on_delivered=on_delivered,
            )
        )

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
    ) -> bool:
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Invio Telegram fallito: %s", exc)
        except Exception:
            logger.exception("Errore inatteso invio Telegram")
        return False

    def notify_event_video(
        self,
        event_id: str,
        class_label: str | None = None,
        video_path: str | None = None,
        on_delivered: Callable[[], None] | None = None,
    ) -> bool:
        """Accoda un video Telegram. ``on_delivered`` viene chiamato solo dopo invio riuscito."""
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

        return self._enqueue(
            _DeliveryJob(
                event_id=event_id,
                class_label=class_label,
                image_path=None,
                video_path=video_path,
                token=token,
                chat_id=chat_id,
                on_delivered=on_delivered,
            )
        )

    def _send_video(
        self,
        token: str,
        chat_id: str,
        event_id: str,
        class_label: str | None,
        video_path: str | None,
    ) -> bool:
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
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError) as exc:
            logger.warning("Invio video Telegram fallito: %s", exc)
        except Exception:
            logger.exception("Errore inatteso invio video Telegram")
        return False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": bool(self._bot_token() and self._chat_id()),
            "classes": sorted(self._allowed_classes()),
            "min_interval_sec": self._min_interval(),
        }
