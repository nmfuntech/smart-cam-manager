"""Telegram push notifications for motion events.

Configuration is read from the environment on every call so that runtime config
updates take effect without a restart. Network I/O runs in a background thread to
avoid blocking the motion detection loop.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from blackframe import telegram_client
from blackframe.envutil import env_bool as _env_bool
from blackframe.envutil import env_str as _env
from blackframe.modes import get_mode_manager
from blackframe.service_layer import _write_private_text
from blackframe.telegram_client import TELEGRAM_API_BASE  # noqa: F401  (re-export)
from blackframe.timewindows import in_window, parse_window

logger = logging.getLogger(__name__)

# Limite prudente per sendVideo (Telegram accetta ~50MB; oltre ~20MB spesso fallisce).
DEFAULT_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Retry: un invio fallito (timeout/blip di rete) viene ri-accodato con backoff
# esponenziale invece di essere scartato. Senza retry una notifica andava persa
# per sempre al primo errore di rete — causa principale delle notifiche mancanti.
DEFAULT_MAX_ATTEMPTS = 4
_RETRY_BACKOFF_BASE_SEC = 5.0
_RETRY_BACKOFF_CAP_SEC = 300.0

# Coda persistita su disco (privata, 0600) così un riavvio non perde le notifiche
# in volo. NON contiene token/chat_id (ri-letti dall'env al caricamento) né la
# callback on_delivered: il dedup definitivo vive comunque in meta.json.
_QUEUE_PATH = Path("data/.telegram_queue.json")
_PERSISTED_FIELDS = ("event_id", "class_label", "image_path", "video_path", "attempts")


@dataclass(frozen=True)
class _DeliveryJob:
    event_id: str
    class_label: str | None
    image_path: str | None
    video_path: str | None
    token: str
    chat_id: str
    on_delivered: Callable[[], None] | None = None
    # Tentativi già consumati per questo job e istante monotòno prima del quale il
    # worker non deve riprovare (backoff). not_before si azzera al reload.
    attempts: int = 0
    not_before: float = 0.0


def telegram_api_call(token: str, method: str, params: dict | None = None) -> dict:
    """Call a Telegram Bot API method (thin wrapper kept for existing callers)."""
    return telegram_client.api_call(token, method, params)


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


class TelegramNotifier:
    def __init__(self, queue_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._last_sent_at: float | None = None
        self._muted_until: float | None = None
        self._pending: list[_DeliveryJob] = []
        self._worker_running = False
        self._queue_path = queue_path or _QUEUE_PATH
        self._state_path = self._queue_path.with_name("notify_state.json")
        self._load_mute_state()
        self._load_queue()
        # Se ci sono notifiche ripristinate dal disco, avvia subito il worker:
        # altrimenti resterebbero in coda finché un nuovo evento non lo accende.
        if self._pending:
            self._start_worker()

    def _max_video_bytes(self) -> int:
        try:
            mb = float(_env("NOTIFY_TELEGRAM_MAX_VIDEO_MB", "20"))
        except ValueError:
            mb = 20.0
        return max(1, int(mb * 1024 * 1024))

    def _max_attempts(self) -> int:
        try:
            return max(1, int(_env("NOTIFY_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))))
        except ValueError:
            return DEFAULT_MAX_ATTEMPTS

    def _max_pending(self) -> int:
        try:
            return max(1, int(_env("NOTIFY_QUEUE_MAX", "64") or 64))
        except ValueError:
            return 64

    def _append_bounded_locked(self, job: _DeliveryJob) -> None:
        while len(self._pending) >= self._max_pending():
            dropped = self._pending.pop(0)
            logger.error(
                "Coda notifiche piena (%d): scarto evento piu vecchio %s",
                self._max_pending(),
                dropped.event_id,
            )
        self._pending.append(job)

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker_running:
                return
            self._worker_running = True
            threading.Thread(target=self._delivery_worker, daemon=True).start()

    def _enqueue(self, job: _DeliveryJob) -> bool:
        self._start_worker()
        with self._lock:
            self._append_bounded_locked(job)
            self._persist_locked()
        return True

    def _pop_ready_job_locked(self, now: float) -> _DeliveryJob | None:
        """Estrae il primo job maturo (not_before<=now). I job in backoff restano."""
        for index, job in enumerate(self._pending):
            if job.not_before <= now:
                return self._pending.pop(index)
        return None

    def _delivery_worker(self) -> None:
        while True:
            if self._is_muted():
                time.sleep(1.0)
                continue
            now = time.monotonic()
            with self._lock:
                job = self._pop_ready_job_locked(now)
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
                    self._persist_locked()
                if job.on_delivered is not None:
                    try:
                        job.on_delivered()
                    except Exception:
                        logger.exception("Callback post-invio Telegram fallito")
            else:
                self._handle_failure(job)

    def _handle_failure(self, job: _DeliveryJob) -> None:
        """Ri-accoda il job con backoff, o lo scarta definitivamente dopo MAX_ATTEMPTS."""
        attempts = job.attempts + 1
        if attempts >= self._max_attempts():
            logger.error(
                "Invio Telegram per %s fallito definitivamente dopo %d tentativi",
                job.event_id,
                attempts,
            )
            with self._lock:
                self._persist_locked()
            return
        backoff = min(_RETRY_BACKOFF_BASE_SEC * (2 ** (attempts - 1)), _RETRY_BACKOFF_CAP_SEC)
        retried = replace(job, attempts=attempts, not_before=time.monotonic() + backoff)
        logger.warning(
            "Invio Telegram per %s non riuscito (tentativo %d), riprovo tra %.0fs",
            job.event_id,
            attempts,
            backoff,
        )
        with self._lock:
            self._append_bounded_locked(retried)
            self._persist_locked()

    # --- persistenza coda ---------------------------------------------------

    def _persist_locked(self) -> None:
        """Salva la coda (senza segreti) su disco. Chiamare col lock acquisito."""
        try:
            payload = [
                {field: getattr(job, field) for field in _PERSISTED_FIELDS} for job in self._pending
            ]
            _write_private_text(self._queue_path, json.dumps(payload))
        except Exception:
            logger.exception("Persistenza coda Telegram fallita")

    def _load_queue(self) -> None:
        """Ricarica la coda da disco all'avvio, riattaccando token/chat_id dall'env."""
        if not self._queue_path.exists():
            return
        token = self._bot_token()
        chat_id = self._chat_id()
        if not token or not chat_id:
            return
        try:
            data = json.loads(self._queue_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Coda Telegram persistita illeggibile, ignorata")
            return
        if not isinstance(data, list):
            return
        restored = 0
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("event_id"):
                continue
            self._append_bounded_locked(
                _DeliveryJob(
                    event_id=str(entry.get("event_id")),
                    class_label=entry.get("class_label"),
                    image_path=entry.get("image_path"),
                    video_path=entry.get("video_path"),
                    token=token,
                    chat_id=chat_id,
                    on_delivered=None,
                    attempts=int(entry.get("attempts", 0) or 0),
                    not_before=0.0,
                )
            )
            restored += 1
        if restored:
            logger.info("Ripristinate %d notifiche Telegram in coda dal disco", restored)

    def _deliver(self, job: _DeliveryJob) -> bool:
        if job.video_path and Path(job.video_path).is_file():
            size = Path(job.video_path).stat().st_size
            too_large = size > self._max_video_bytes()
            if not too_large:
                if self._send_video(
                    job.token, job.chat_id, job.event_id, job.class_label, job.video_path
                ):
                    return True
                # Invio video fallito per rete/timeout: ritenta il video con backoff.
                # Il ripiego sulla foto di copertina avviene solo all'ultimo tentativo,
                # altrimenti un blip di rete costava per sempre la clip.
                if job.attempts + 1 < self._max_attempts():
                    return False
                reason = "invio video non riuscito dopo i tentativi"
            else:
                reason = f"clip troppo grande ({size // (1024 * 1024)} MB)"
            cover = Path(job.video_path).parent / "cover.jpg"
            if cover.is_file():
                logger.info("Telegram %s: %s, invio foto di copertina", job.event_id, reason)
                return self._send(job.token, job.chat_id, job.event_id, job.class_label, str(cover))
            logger.warning("Telegram %s: %s e nessuna cover.jpg", job.event_id, reason)
            return self._send(job.token, job.chat_id, job.event_id, job.class_label, None)
        return self._send(job.token, job.chat_id, job.event_id, job.class_label, job.image_path)

    @property
    def enabled(self) -> bool:
        return _env_bool("NOTIFY_TELEGRAM_ENABLED", False)

    def mute(self, seconds: float) -> float:
        """Silenzia temporaneamente le notifiche. seconds<=0 annulla la pausa.

        La pausa sopravvive a un riavvio (salvata in notify_state.json).
        Ritorna i secondi residui di pausa (0 se non in pausa).
        """
        with self._lock:
            if seconds > 0:
                self._muted_until = time.monotonic() + seconds
            else:
                self._muted_until = None
        self._save_mute_state(seconds if seconds > 0 else 0)
        return self.muted_remaining()

    def _save_mute_state(self, seconds: float) -> None:
        until = time.time() + seconds if seconds > 0 else None
        try:
            _write_private_text(self._state_path, json.dumps({"muted_until": until}))
        except Exception:
            logger.warning("Stato pausa notifiche non salvato")

    def _load_mute_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        until = data.get("muted_until") if isinstance(data, dict) else None
        if isinstance(until, (int, float)):
            remaining = until - time.time()
            if remaining > 0:
                self._muted_until = time.monotonic() + remaining
                logger.info("Pausa notifiche ripristinata: %.0f min residui", remaining / 60)

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
            return max(0.0, float(_env("NOTIFY_MIN_INTERVAL_SEC", "5")))
        except ValueError:
            return 5.0

    def notify_event(
        self,
        event_id: str,
        class_label: str | None = None,
        image_path: str | None = None,
        on_delivered: Callable[[], None] | None = None,
    ) -> bool:
        """Accoda una notifica. ``on_delivered`` è chiamato solo dopo invio riuscito."""
        if not self.enabled or self._is_muted():
            return False
        if not get_mode_manager().allows_alerts():
            logger.info("Avviso %s non inviato: modalità %s", event_id, get_mode_manager().mode)
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

    def _alert_markup(self, event_id: str) -> dict | None:
        """Bottoni rapidi sotto l'avviso (gestiti dal bot comandi Telegram)."""
        if not _env_bool("NOTIFY_ALERT_BUTTONS", True):
            return None
        if not _env_bool("TELEGRAM_COMMANDS_ENABLED", False):
            return None  # nessuno risponderebbe ai bottoni
        base = event_id.replace("motion_event_", "").split("__", 1)[0]
        return {
            "inline_keyboard": [
                [
                    {"text": "📸 Live", "callback_data": "/snapshot"},
                    {"text": "🎬 Clip 10s", "callback_data": "/clip 10"},
                ],
                [
                    {"text": "🔕 Pausa 1h", "callback_data": "/mute 60"},
                    {"text": "❌ Falso allarme", "callback_data": f"/false_alarm {base}"[:64]},
                ],
            ]
        }

    def _is_quiet(self, class_label: str | None) -> bool:
        """Notifica silenziosa (senza suono) negli orari di quiete.

        Le persone non vengono mai silenziate di default: le categorie che
        diventano silenziose sono in NOTIFY_QUIET_CLASSES.
        """
        window = parse_window(_env("NOTIFY_QUIET_HOURS"))
        if window is None or not in_window(window):
            return False
        raw = _env("NOTIFY_QUIET_CLASSES", "animale_domestico,movimento")
        quiet_classes = {item.strip() for item in raw.split(",") if item.strip()}
        return (class_label or "movimento") in quiet_classes

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
        options = {
            "reply_markup": self._alert_markup(event_id),
            "silent": self._is_quiet(class_label),
        }
        try:
            if image_path and Path(image_path).is_file():
                result = telegram_client.send_photo(
                    token, chat_id, path=image_path, caption=caption, timeout=30, **options
                )
            else:
                result = telegram_client.send_message(token, chat_id, caption, **options)
        except Exception:
            logger.exception("Errore inatteso invio Telegram")
            return False
        if not result.get("ok"):
            logger.warning("Invio Telegram fallito: %s", result.get("description"))
            return False
        return True

    def notify_event_video(
        self,
        event_id: str,
        class_label: str | None = None,
        video_path: str | None = None,
        on_delivered: Callable[[], None] | None = None,
    ) -> bool:
        """Accoda un video. ``on_delivered`` è chiamato solo dopo invio riuscito."""
        if not self.enabled or self._is_muted():
            return False
        if not get_mode_manager().allows_alerts():
            logger.info("Avviso %s non inviato: modalità %s", event_id, get_mode_manager().mode)
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
        options = {
            "reply_markup": self._alert_markup(event_id),
            "silent": self._is_quiet(class_label),
        }
        try:
            if video_path and Path(video_path).is_file():
                result = telegram_client.send_video(
                    token, chat_id, path=video_path, caption=caption, **options
                )
            else:
                result = telegram_client.send_message(token, chat_id, caption, **options)
        except Exception:
            logger.exception("Errore inatteso invio video Telegram")
            return False
        if not result.get("ok"):
            logger.warning("Invio video Telegram fallito: %s", result.get("description"))
            return False
        return True

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": bool(self._bot_token() and self._chat_id()),
            "classes": sorted(self._allowed_classes()),
            "min_interval_sec": self._min_interval(),
        }
