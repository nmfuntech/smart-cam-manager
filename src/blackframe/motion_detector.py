"""Motion detection loop: MOG2 foreground, event lifecycle, classification, alerts."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import cv2

from blackframe.automation import EventContext
from blackframe.camera_stream import CameraStream
from blackframe.classification import PersonPetClassifier
from blackframe.envutil import env_float, env_int
from blackframe.modes import get_mode_manager
from blackframe.motion_events import MotionEventStore
from blackframe.motion_zones import ZoneMaskCache

logger = logging.getLogger(__name__)


class MotionDetector:
    def __init__(
        self,
        camera_stream: CameraStream,
        config: dict,
        notifier=None,
        recorder=None,
        automation=None,
        camera_id: str | None = None,
    ):
        self.camera_stream = camera_stream
        self.config = config
        self.event_store = MotionEventStore(config)
        self.classifier = PersonPetClassifier.from_config(config)
        self.notifier = notifier
        self.recorder = recorder
        self.automation = automation
        # Id del profilo camera sorgente: finisce in EventContext.source così le
        # regole di automazione con filtro `source:` possono scattare per camera.
        self.camera_id = camera_id
        self._zones = ZoneMaskCache(camera_id or "default")
        self._classified_events: set[str] = set()
        self._notified_events: set[str] = set()
        self._finalized_events: set[str] = set()
        self._last_classified_notify_at: float | None = None
        self._last_notify_at: float | None = None
        self._CLIP_CLASSIFY_MAX_FRAMES = 24
        # Automazione a bassa latenza: scatta durante l'evento appena un frame
        # contiene un soggetto, senza attendere la chiusura/registrazione/transcodifica.
        self._automation_fired: set[str] = set()
        self._live_classify_attempts: dict[str, int] = {}
        self._LIVE_CLASSIFY_MAX = 12
        self._stopped = threading.Event()
        self.lock = threading.Lock()
        # Coda dei task pesanti (I/O su disco + inferenza ONNX): eseguiti su un thread
        # daemon separato così non vengono mai svolti sotto self.lock né sul thread di
        # detection. Il thread di detection resta reattivo e get_status() non si blocca
        # mai dietro un salvataggio o una classificazione. Task "critical" (apertura/
        # chiusura evento) non vengono mai scartati; i salvataggi frame intermedi sì,
        # se la coda è sovraccarica (backpressure).
        self._event_queue: deque[tuple[bool, Callable[[], None]]] = deque()
        self._event_queue_lock = threading.Lock()
        self._event_queue_ready = threading.Condition(self._event_queue_lock)
        self.processed_frames = 0
        self.trigger_streak = 0
        self.clear_streak = 0
        self.motion_detected = False
        self.last_motion_at = None
        self.last_motion_at_display = None
        self.current_area = 0.0
        self.last_trigger_area = 0.0
        self.last_capture_path = None
        self.last_event_id = None
        self.last_error = ""
        self.last_capture_saved_at = None
        # Normalized (x, y, w, h) of the latest motion bbox, used to crop the
        # classified frame down to the moving subject. None until first motion.
        self._last_motion_rect_norm = None
        self._bg_subtractor = None
        self._kernel_open = None
        self._kernel_dilate = None
        self._needs_subtractor_rebuild = False
        self._build_subtractor()
        self._restore_last_capture()
        self._warn_if_classification_unready()
        self._event_thread = threading.Thread(
            target=self._event_worker_loop, daemon=True, name="motion-event-worker"
        )
        self._event_thread.start()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _warn_if_classification_unready(self) -> None:
        """Log a clear warning when classification is on but no usable model is present."""
        classifier = getattr(self, "classifier", None)
        if classifier is not None and classifier.enabled and not classifier.ready:
            logger.warning(
                "Classificazione abilitata (backend %s) ma il modello non è disponibile: "
                "gli eventi resteranno '%s'. Fornisci il modello (vedi "
                "CLASSIFICATION_*_MODEL_PATH, cartella models/) oppure imposta "
                "CLASSIFICATION_ENABLED=false.",
                classifier.backend_name,
                PersonPetClassifier.LABEL_UNKNOWN,
            )

    def _build_subtractor(self) -> None:
        """(Re)build the MOG2 subtractor and morphology kernels. Runs only on the _run thread."""
        self._bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=int(self.config.get("mog2_history", 500)),
            varThreshold=float(self.config.get("mog2_var_threshold", 35)),
            detectShadows=True,
        )
        k = int(self.config.get("morph_kernel", 3))
        if k % 2 == 0:
            k += 1
        self._kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self._kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self.processed_frames = 0
        self._needs_subtractor_rebuild = False

    def _current_learning_rate(self) -> float:
        if self.motion_detected:
            # Almost frozen during an active event so a still subject is not absorbed.
            return float(self.config.get("learning_rate_active", 0.0005))
        return float(self.config.get("learning_rate", -1))

    def _preprocess(self, frame):
        w = int(self.config.get("scale_width", 0) or 0)
        if w and frame.shape[1] > w:
            h = int(frame.shape[0] * w / frame.shape[1])
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(
            gray,
            (self.config["blur_size"], self.config["blur_size"]),
            0,
        )

    def _detect_motion(self, frame):
        """Pixel-level motion detection. Returns (motion_now, largest_usable_area, frame_area).

        Pure detection: no lock, no streak state. Runs only on the _run thread because
        the MOG2 subtractor is stateful.
        """
        processed = self._preprocess(frame)
        frame_area = float(processed.shape[0] * processed.shape[1])

        fg = self._bg_subtractor.apply(processed, learningRate=self._current_learning_rate())
        # MOG2 marks foreground as 255 and shadows as 127; keep only solid foreground.
        mask = cv2.inRange(fg, 250, 255)
        # User-drawn exclusion zones (windows, TV, mirrors): drop their pixels and
        # measure ratios against the watched area only.
        zones = getattr(self, "_zones", None)
        zone_mask, watched_area = (
            zones.get(processed.shape[:2], time.monotonic()) if zones else (None, frame_area)
        )
        if zone_mask is not None:
            mask = cv2.bitwise_and(mask, zone_mask)
            frame_area = max(1.0, watched_area)

        # Global-change guard: a lighting/exposure shift lights up most of the frame.
        global_ratio = self.config.get("global_change_ratio", 0.5)
        if global_ratio > 0 and cv2.countNonZero(mask) > frame_area * global_ratio:
            return False, 0.0, frame_area

        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._kernel_open)
        dilate_iter = int(self.config.get("morph_dilate_iter", 2))
        if dilate_iter > 0:
            mask = cv2.dilate(mask, self._kernel_dilate, iterations=dilate_iter)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        largest_usable_area = 0.0
        largest_contour = None
        motion_now = False
        for contour in contours:
            area = cv2.contourArea(contour)
            if self._is_usable_motion_area(area, frame_area):
                if area > largest_usable_area:
                    largest_usable_area = area
                    largest_contour = contour
                motion_now = True
        if largest_contour is not None:
            # Store the bbox normalized to the processed frame so it maps onto a
            # full-res frame of any resolution (aspect ratio is preserved).
            ph, pw = processed.shape[:2]
            x, y, w, h = cv2.boundingRect(largest_contour)
            self._last_motion_rect_norm = (x / pw, y / ph, w / pw, h / ph)
        return motion_now, largest_usable_area, frame_area

    def _get_event_store(self) -> MotionEventStore:
        if not hasattr(self, "event_store") or self.event_store is None:
            self.event_store = MotionEventStore(self.config)
        return self.event_store

    def _start_event_recording(self, event_dir) -> None:
        if self.recorder is not None and self.recorder.enabled and event_dir is not None:
            self.recorder.start_event(event_dir)

    def _save_motion_frame(self, frame, timestamp: str) -> str | None:
        store = self._get_event_store()
        filepath, event_id = store.save_frame(frame, timestamp)
        self.last_event_id = event_id
        self.last_capture_saved_at = time.time()
        if event_id:
            self._start_event_recording(store.current_event_dir)
        # Classification is subordinate to motion: it only ever runs as part of a motion
        # event. The _run loop already short-circuits when motion is disabled; this guard
        # makes the dependency explicit so classification can never run on its own.
        classification_on = (
            bool(self.config.get("enabled", True))
            and hasattr(self, "classifier")
            and self.classifier is not None
            and self.classifier.enabled
        )
        if event_id and classification_on:
            self._classify_event_frame(event_id, frame)
        return filepath

    def _prefer_video_notify(self) -> bool:
        return (
            self.recorder is not None
            and self.recorder.enabled
            and bool(self.config.get("notify_prefer_video", True))
        )

    def _already_notified(self, event_id: str) -> bool:
        """Dedup notifications: in-memory fast path + on-disk marker surviving restarts."""
        if event_id in self._notified_events:
            return True
        store = self._get_event_store()
        if store.event_was_notified(event_id):
            return True
        event_dir = store.find_event_dir(event_id)
        if event_dir is not None and event_dir.name != event_id:
            return event_dir.name in self._notified_events or store.event_was_notified(
                event_dir.name
            )
        return False

    def _mark_notified(self, event_id: str) -> None:
        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        resolved_id = event_dir.name if event_dir is not None else event_id
        self._notified_events.add(resolved_id)
        if resolved_id != event_id:
            self._notified_events.add(event_id)
        store.mark_event_notified(resolved_id)

    def _note_classified_notification(self, class_label: str | None) -> None:
        if class_label in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            self._last_classified_notify_at = time.monotonic()

    def _note_notification_sent(self) -> None:
        self._last_notify_at = time.monotonic()

    def _is_within_notify_burst(self) -> bool:
        last = getattr(self, "_last_notify_at", None)
        if last is None:
            return False
        return (time.monotonic() - last) < float(self.config.get("notify_burst_sec", 20))

    def _is_tail_motion_notification(self, classification: dict | None) -> bool:
        """Movimento generico subito dopo un alert persona/pet (stesso passaggio)."""
        classification = classification or {}
        if classification.get("detected_label") in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            return False
        status = classification.get("classification_status")
        if status not in ("no_detection", "motion_only", "unknown"):
            return False
        window = float(self.config.get("notify_tail_suppress_sec", 15))
        last = getattr(self, "_last_classified_notify_at", None)
        return last is not None and (time.monotonic() - last) < window

    def _resolve_event_label(self, classification: dict | None) -> str:
        """Category used for the folder name / archive: the confidently recognized
        class (person/pet, even if 'ignored'), otherwise a generic 'movimento' tag."""
        detected = (classification or {}).get("detected_label")
        if detected in (PersonPetClassifier.LABEL_PERSONA, PersonPetClassifier.LABEL_PET):
            return detected
        return "movimento"

    def _notification_class_label(self, classification: dict | None) -> str | None:
        """Etichetta per Telegram: preferisce la categoria riconosciuta (detected_label)."""
        classification = classification or {}
        label = classification.get("detected_label") or classification.get("class_label")
        if label in (PersonPetClassifier.LABEL_UNKNOWN, "unknown", None, ""):
            return None
        return label

    def _make_event_complete_callback(
        self, event_id: str, label: str, classification: dict | None, should_notify: bool
    ):
        """Runs after the recorder finalizes (or abandons) the clip."""

        def _callback(video_path: Path | None):
            self._finalize_closed_event_notification(
                event_id,
                label,
                classification,
                should_notify=should_notify,
                video_path=video_path,
            )

        return _callback

    def _classify_best_from_event(self, event_dir: Path) -> dict | None:
        """Scansiona la clip evento e sceglie il miglior rilevamento persona/pet.

        Il primo fotogramma (preroll) spesso non contiene ancora il soggetto: classificare
        solo cover.jpg o il frame iniziale produce falsi ``no_detection``.

        Questo è il burst CPU peggiore sul mini PC (decode video + inferenze ONNX
        in serie), quindi è contenuto in tre modi: tetto di frame configurabile,
        stride (un frame classificato ogni due, ``grab()`` scarta l'altro senza
        decodifica completa) e uscita anticipata quando un soggetto è già stato
        trovato con confidenza alta — altri frame non cambierebbero l'esito.
        """
        if not self.classifier.enabled or not self.classifier.ready:
            return None
        max_frames = env_int(
            "CLASSIFICATION_CLIP_MAX_FRAMES", getattr(self, "_CLIP_CLASSIFY_MAX_FRAMES", 24)
        )
        early_exit_conf = env_float("CLASSIFICATION_EARLY_EXIT_CONF", 0.85)
        best: dict | None = None
        best_conf = -1.0
        clip = event_dir / "event.mp4"
        if clip.is_file():
            capture = cv2.VideoCapture(str(clip))
            try:
                for _ in range(max_frames):
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break
                    result = self.classifier.classify(frame)
                    if result and result.get("classification_status") == "ok":
                        label = result.get("detected_label")
                        conf = float(result.get("confidence") or 0)
                        if (
                            label
                            in (
                                PersonPetClassifier.LABEL_PERSONA,
                                PersonPetClassifier.LABEL_PET,
                            )
                            and conf > best_conf
                        ):
                            best = result
                            best_conf = conf
                    if best is not None and best_conf >= early_exit_conf:
                        break
                    # Stride: salta il frame successivo senza decodificarlo.
                    capture.grab()
            finally:
                capture.release()
        if best is not None:
            return best
        cover = event_dir / "cover.jpg"
        if cover.is_file():
            frame = cv2.imread(str(cover))
            if frame is not None:
                return self.classifier.classify(frame)
        return None

    def _maybe_classify_closed_event(
        self, event_id: str, classification: dict | None
    ) -> tuple[dict | None, str]:
        """Classifica a chiusura scansionando la clip (non solo il primo frame)."""
        classification = dict(classification) if classification else {}
        if classification.get("detected_label") in (
            PersonPetClassifier.LABEL_PERSONA,
            PersonPetClassifier.LABEL_PET,
        ):
            return classification, self._resolve_event_label(classification)

        store = self._get_event_store()
        store.ensure_preview_image(event_id)
        event_dir = store.find_event_dir(event_id)
        if event_dir is None or not self.classifier.enabled or not self.classifier.ready:
            return classification, self._resolve_event_label(classification)

        result = self._classify_best_from_event(event_dir)
        if not result:
            return classification, self._resolve_event_label(classification)
        logger.info(
            "Classificazione evento %s: label=%s conf=%s status=%s",
            event_id,
            result.get("detected_label"),
            result.get("confidence"),
            result.get("classification_status"),
        )
        store.save_event_meta(event_id, {"classification": result})
        return result, self._resolve_event_label(result)

    def _finalize_closed_event_notification(
        self,
        event_id: str | None,
        label: str,
        classification: dict | None,
        *,
        should_notify: bool,
        video_path: Path | None = None,
    ) -> str | None:
        """Rename a closed event and send Telegram once it is visible in the archive."""
        if not event_id:
            return None
        store = self._get_event_store()
        store.ensure_preview_image(event_id)
        classification, label = self._maybe_classify_closed_event(event_id, classification)
        should_notify = self._should_notify_for(classification or {})
        resolved_id = store.rename_event_with_label(event_id, label)
        logger.info(
            "Evento finalizzato: %s label=%s notify=%s",
            resolved_id,
            label,
            should_notify,
        )
        self._emit_automation(resolved_id, label, video_path)
        if not should_notify or self.notifier is None:
            return resolved_id
        if not self._should_notify_for(classification):
            return resolved_id
        self._deliver_event_notification(resolved_id, classification, video_path)
        return resolved_id

    def _emit_automation(self, event_id: str | None, label: str, video_path: Path | None) -> None:
        automation = getattr(self, "automation", None)
        if automation is None or not event_id:
            return
        # Già scattata durante l'evento (percorso a bassa latenza): non rifare.
        if self._event_base_id(event_id) in self._automation_fired:
            return
        try:
            ctx = EventContext(
                event_id=event_id,
                category=label,
                # getattr: i test costruiscono detector via __new__ senza __init__.
                source=getattr(self, "camera_id", None),
                timestamp=time.time(),
                video_path=str(video_path) if video_path else None,
            )
            automation.emit(ctx)
        except Exception:
            logger.exception("Automation emit fallito per evento %s", event_id)

    def _deliver_event_notification(
        self,
        event_id: str,
        classification: dict | None,
        video_path: Path | None,
    ) -> None:
        """Send video when available and preferred; otherwise fall back to cover photo."""
        if self.notifier is None or not event_id or self._already_notified(event_id):
            return

        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        resolved_id = event_dir.name if event_dir is not None else event_id
        class_label = self._notification_class_label(classification)
        base_dir = event_dir or Path(self.config["save_dir"]) / resolved_id
        # Resolve the clip after any category rename — the callback may still hold
        # the pre-rename path, which no longer exists once the folder is suffixed.
        clip = base_dir / "event.mp4"

        if self._prefer_video_notify() and clip.is_file():
            try:
                if self.notifier.notify_event_video(
                    event_id=resolved_id,
                    class_label=class_label,
                    video_path=str(clip),
                    on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
                ):
                    self._mark_notified(resolved_id)
                    self._note_classified_notification(class_label)
                    self._note_notification_sent()
                    return
            except Exception:
                logger.exception("Invio video Telegram fallito")

        cover = base_dir / "cover.jpg"
        image_path = str(cover) if cover.exists() else None
        try:
            if self.notifier.notify_event(
                event_id=resolved_id,
                class_label=class_label,
                image_path=image_path,
                on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
            ):
                self._mark_notified(resolved_id)
                self._note_classified_notification(class_label)
                self._note_notification_sent()
        except Exception:
            logger.exception("Invio notifica evento fallito")

    def _notify_event(self, event_id: str, classification: dict | None) -> None:
        if self.notifier is None or not event_id:
            return
        if self._already_notified(event_id):
            return
        class_label = self._notification_class_label(classification)
        store = self._get_event_store()
        event_dir = store.find_event_dir(event_id)
        cover = (event_dir or Path(self.config["save_dir"]) / event_id) / "cover.jpg"
        image_path = str(cover) if cover.exists() else None
        resolved_id = event_dir.name if event_dir is not None else event_id
        try:
            self.notifier.notify_event(
                event_id=resolved_id,
                class_label=class_label,
                image_path=image_path,
                on_delivered=lambda eid=resolved_id: self._mark_notified(eid),
            )
        except Exception:
            logger.exception("Invio notifica evento fallito")

    # Outcomes that should NOT raise an alert: a person/pet of a disabled category
    # ("ignored"), or a detection too weak ("low_confidence"). Plain motion without
    # a recognized subject ("no_detection") still notifies — the motion pipeline
    # already filtered the event and it appears in the archive.
    _NON_NOTIFY_CLASSIFICATION_STATUSES = frozenset({"low_confidence", "ignored"})

    def _classification_allows_notify(self, status: str | None) -> bool:
        return status not in self._NON_NOTIFY_CLASSIFICATION_STATUSES

    def _should_notify_for(self, classification: dict | None) -> bool:
        """Whether an event may raise an alert, re-checked at notification time.

        Beyond the frozen status, it also re-applies the per-category filter against
        the *current* classifier targets: toggling a category off mid-event then
        suppresses an event that was classified while that category was still on.
        """
        classification = classification or {}
        if not self._classification_allows_notify(classification.get("classification_status")):
            return False
        if self._is_within_notify_burst():
            return False
        if self._is_tail_motion_notification(classification):
            return False
        detected = classification.get("detected_label")
        classifier = getattr(self, "classifier", None)
        if detected and classifier is not None and detected not in classifier.targets:
            return False
        return True

    def _read_event_classification(self, event_id: str | None) -> dict:
        if not event_id:
            return {}
        try:
            import json

            event_dir = self._get_event_store().find_event_dir(event_id)
            if event_dir is None:
                return {}
            meta_path = event_dir / "meta.json"
            if not meta_path.exists():
                return {}
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data.get("classification") or {}
        except Exception:
            return {}

    def _get_last_class_label(self, event_id: str | None) -> str | None:
        return self._read_event_classification(event_id).get("class_label")

    def _crop_to_motion(self, frame):
        """Crop the full-res frame to the latest motion bbox (with padding).

        Focusing the detector on the moving subject improves accuracy when the
        subject is small in the scene and trims work. Returns the original frame
        when cropping is disabled or no motion bbox is known yet.
        """
        if not self.config.get("classification_crop_to_motion", True):
            return frame
        rect = self._last_motion_rect_norm
        if rect is None:
            return frame
        h, w = frame.shape[:2]
        pad = float(self.config.get("classification_crop_padding", 0.2))
        nx, ny, nw, nh = rect
        x1 = int(max(0.0, nx - nw * pad) * w)
        y1 = int(max(0.0, ny - nh * pad) * h)
        x2 = int(min(1.0, nx + nw * (1.0 + pad)) * w)
        y2 = int(min(1.0, ny + nh * (1.0 + pad)) * h)
        if x2 <= x1 or y2 <= y1:
            return frame
        return frame[y1:y2, x1:x2]

    @staticmethod
    def _event_base_id(event_id: str | None) -> str:
        """Id evento senza il suffisso categoria (``__persona``), stabile tra rename."""
        if not event_id:
            return ""
        base, _, _ = event_id.partition(MotionEventStore.LABEL_SEPARATOR)
        return base

    def _fire_live_automation(self, event_id: str, detected_label: str) -> None:
        """Fa scattare l'automazione durante l'evento (una sola volta per evento)."""
        automation = getattr(self, "automation", None)
        if automation is None:
            return
        base_id = self._event_base_id(event_id)
        if base_id in self._automation_fired:
            return
        self._automation_fired.add(base_id)
        try:
            automation.emit(
                EventContext(
                    event_id=event_id,
                    category=detected_label,
                    source=getattr(self, "camera_id", None),
                    timestamp=time.time(),
                )
            )
            logger.info("Automazione live: %s (%s)", event_id, detected_label)
        except Exception:
            logger.exception("Automazione live fallita per evento %s", event_id)

    def _classify_event_frame(self, event_id: str, frame) -> None:
        # Classifica i frame salvati durante l'evento e, appena trova un soggetto,
        # accende subito l'automazione. Riprova sui frame successivi finché ottiene
        # solo 'no_detection' (la persona spesso entra dopo i primi frame), con un
        # tetto di tentativi per non classificare all'infinito un puro movimento.
        if event_id in self._classified_events:
            return
        if self.classifier.sample_policy != "event_cover":
            return
        result = self.classifier.classify(self._crop_to_motion(frame))
        if result is None:
            return
        detected = result.get("detected_label")
        is_subject = detected in (PersonPetClassifier.LABEL_PERSONA, PersonPetClassifier.LABEL_PET)
        store = self._get_event_store()
        attempts = self._live_classify_attempts.get(event_id, 0) + 1
        self._live_classify_attempts[event_id] = attempts
        # Persisti il primo esito (così meta esiste sempre); riscrivi solo per
        # "promuovere" un esito non-soggetto a soggetto, evitando write ridondanti.
        if attempts == 1 or is_subject:
            store.save_event_meta(event_id, {"classification": result})
        if is_subject:
            # Soggetto riconosciuto: blocca la classificazione e accendi subito.
            self._classified_events.add(event_id)
            self._fire_live_automation(event_id, detected)
            return
        if attempts >= self._LIVE_CLASSIFY_MAX:
            self._classified_events.add(event_id)
        # Alerts are sent only after the event closes so Telegram and the UI archive
        # stay aligned (.closed marker exists before we notify).

    def _should_save_active_event_frame(self, now: float) -> bool:
        if not self.motion_detected:
            return False
        if self.clear_streak >= self.config["clear_frames"]:
            return False
        if self.last_capture_saved_at is None:
            return True
        return now - self.last_capture_saved_at >= self.config.get(
            "capture_interval", self.config["min_interval"]
        )

    def _is_usable_motion_area(self, area: float, frame_area: float) -> bool:
        if area < self.config["min_area"]:
            return False
        max_ratio = self.config.get("max_area_ratio", 1.0)
        if max_ratio <= 0:
            return True
        return area <= frame_area * max_ratio

    def _restore_last_capture(self) -> None:
        event = self._get_event_store().latest_event()
        if event:
            self.last_capture_path = event["preview_path"]
            self.last_event_id = event["id"]
            timestamp = event.get("timestamp")
            if timestamp is not None:
                self.last_motion_at_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    def stop(self) -> None:
        self._stopped.set()
        with self._event_queue_ready:
            self._event_queue_ready.notify_all()
        for thread in (getattr(self, "thread", None), getattr(self, "_event_thread", None)):
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=2.0)

    # --- worker eventi (I/O + classificazione fuori dal lock di detection) ---

    # Oltre questa profondità di coda i task non critici (salvataggi frame intermedi)
    # vengono scartati: su un mini PC sotto carico è meglio perdere un frame che far
    # crescere la coda senza limite. Apertura/chiusura evento restano sempre.
    _EVENT_QUEUE_MAX = 8
    _EVENT_QUEUE_TOTAL_MAX = 12

    def _enqueue_event(self, fn: Callable[[], None], *, critical: bool = False) -> bool:
        with self._event_queue_ready:
            noncritical = sum(1 for queued_critical, _ in self._event_queue if not queued_critical)
            if not critical and (
                noncritical >= self._EVENT_QUEUE_MAX
                or len(self._event_queue) >= self._EVENT_QUEUE_TOTAL_MAX
            ):
                logger.warning("Coda eventi piena: salvataggio frame scartato (backpressure)")
                return False
            if critical and len(self._event_queue) >= self._EVENT_QUEUE_TOTAL_MAX:
                # Preserve lifecycle operations by evicting one intermediate frame.
                evict_index = next(
                    (
                        i
                        for i, (queued_critical, _) in enumerate(self._event_queue)
                        if not queued_critical
                    ),
                    None,
                )
                if evict_index is None:
                    logger.error("Coda eventi critica piena: task lifecycle scartato")
                    return False
                del self._event_queue[evict_index]
            self._event_queue.append((critical, fn))
            self._event_queue_ready.notify()
            return True

    def _event_worker_loop(self) -> None:
        while True:
            with self._event_queue_ready:
                while not self._event_queue and not self._stopped.is_set():
                    self._event_queue_ready.wait()
                if self._stopped.is_set() and not self._event_queue:
                    return
                item = self._event_queue.popleft()
            _critical, fn = item
            try:
                fn()
            except Exception:
                logger.exception("Task evento fallito sul worker")

    def _task_open_event(self, early_ts: str) -> None:
        store = self._get_event_store()
        early_id, early_dir = store.open_event(early_ts)
        if early_id:
            # Written on the worker thread, read by _run under the same lock.
            with self.lock:
                self.last_event_id = early_id
            logger.info("Evento movimento aperto: %s", early_id)
        self._start_event_recording(early_dir)

    def _task_open_and_save_frame(self, frame, timestamp: str) -> None:
        """Apre l'evento e salva il primo frame confermato nello stesso task."""
        self._task_open_event(timestamp)
        self._task_save_frame(frame, timestamp)

    def _task_save_frame(self, frame, timestamp: str) -> None:
        self.last_capture_path = self._save_motion_frame(frame, timestamp)

    def _task_close_event(self, closed_event_id: str | None) -> None:
        if not closed_event_id or closed_event_id in self._finalized_events:
            return
        self._finalized_events.add(closed_event_id)
        logger.info("Evento movimento in chiusura: %s", closed_event_id)
        closed_classification = self._read_event_classification(closed_event_id)
        store = self._get_event_store()
        store.close_current_event()
        # Every event must carry a label. With classification off there is no meta yet:
        # stamp a generic 'motion only' tag.
        if closed_event_id and not closed_classification:
            closed_classification = {
                "class_label": "movimento",
                "detected_label": None,
                "classification_status": "motion_only",
            }
            store.save_event_meta(closed_event_id, {"classification": closed_classification})
        label = self._resolve_event_label(closed_classification)
        classification_on = (
            bool(self.config.get("enabled", True))
            and self.classifier is not None
            and self.classifier.enabled
        )
        should_notify = True
        if classification_on:
            should_notify = self._should_notify_for(closed_classification)
        if self.recorder is not None and self.recorder.enabled:
            # Rename and notify happen in the callback, after the clip is finalized
            # (or after we know the clip is missing).
            self.recorder.stop_event(
                on_complete=self._make_event_complete_callback(
                    closed_event_id,
                    label,
                    closed_classification,
                    should_notify,
                )
            )
        else:
            self._finalize_closed_event_notification(
                closed_event_id,
                label,
                closed_classification,
                should_notify=should_notify,
            )
            if self.recorder is not None:
                self.recorder.stop_event()

    def _run(self) -> None:
        last_sequence = -1
        while not self._stopped.is_set():
            disarmed = not get_mode_manager().allows_detection()
            if not self.config["enabled"] or disarmed:
                with self.lock:
                    self.motion_detected = False
                    self.last_error = (
                        "Sistema disarmato" if disarmed else "Rilevamento movimento disabilitato"
                    )
                    self.trigger_streak = 0
                    self.clear_streak = 0
                time.sleep(1)
                continue

            if self._needs_subtractor_rebuild:
                self._build_subtractor()

            get_if_new = getattr(self.camera_stream, "get_raw_frame_if_new", None)
            if callable(get_if_new):
                frame, frame_sequence = get_if_new(last_sequence)
            else:
                frame, frame_sequence = self.camera_stream.get_raw_frame_packet()
            if frame is None:
                time.sleep(self.config["min_interval"])
                continue
            if frame_sequence == last_sequence:
                # Same frame as last iteration (stream slower than the loop, or
                # stalled): re-running MOG2 on it only burns CPU on the mini PC.
                time.sleep(self.config["min_interval"])
                continue
            last_sequence = frame_sequence

            try:
                motion_now, largest_usable_area, frame_area = self._detect_motion(frame)

                now = time.time()
                # Sotto il lock: SOLO la macchina a stati (contatori/flag letti da
                # get_status). Le azioni pesanti (apertura/chiusura evento, salvataggio
                # frame, classificazione, finalize+notifica) vengono raccolte qui come
                # closure e accodate al worker DOPO aver rilasciato il lock.
                deferred: list[tuple[bool, Callable[[], None]]] = []
                with self.lock:
                    self.processed_frames += 1
                    self.current_area = (
                        largest_usable_area
                        if largest_usable_area >= self.config["min_area"]
                        else 0.0
                    )
                    self.last_error = ""
                    if self.processed_frames <= self.config["warmup_frames"]:
                        self.motion_detected = False
                        self.trigger_streak = 0
                        self.clear_streak = 0
                    elif motion_now:
                        self.trigger_streak += 1
                        self.clear_streak = 0
                        if self.trigger_streak >= self.config["trigger_frames"]:
                            self.motion_detected = True
                            self.last_motion_at = now
                            self.last_trigger_area = largest_usable_area
                            now_dt = datetime.now()
                            self.last_motion_at_display = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                            ts = now_dt.strftime("%Y%m%d_%H%M%S")
                            self.last_capture_saved_at = now
                            first_confirmed = self.trigger_streak == self.config["trigger_frames"]
                            if first_confirmed:
                                deferred.append(
                                    (
                                        True,
                                        lambda f=frame, t=ts: self._task_open_and_save_frame(f, t),
                                    )
                                )
                            else:
                                deferred.append(
                                    (
                                        False,
                                        lambda f=frame, t=ts: self._task_save_frame(f, t),
                                    )
                                )
                    else:
                        self.trigger_streak = 0
                        self.clear_streak += 1
                        if self._should_save_active_event_frame(now):
                            now_dt = datetime.now()
                            self.last_motion_at_display = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                            ts = now_dt.strftime("%Y%m%d_%H%M%S")
                            self.last_capture_saved_at = now
                            deferred.append(
                                (False, lambda f=frame, t=ts: self._task_save_frame(f, t))
                            )
                        enough_quiet = self.clear_streak >= self.config["clear_frames"]
                        out_of_cooldown = (
                            self.last_motion_at is None
                            or now - self.last_motion_at >= self.config["cooldown"]
                        )
                        has_open_event = self.last_event_id is not None and (
                            self.motion_detected
                            or self._get_event_store().current_event_dir is not None
                        )
                        if enough_quiet and out_of_cooldown and has_open_event:
                            closed_event_id = self.last_event_id
                            self.last_event_id = None
                            self.motion_detected = False
                            deferred.append(
                                (True, lambda eid=closed_event_id: self._task_close_event(eid))
                            )

                # Fuori dal lock: accoda i task pesanti al worker (FIFO, serializzati).
                for critical, fn in deferred:
                    self._enqueue_event(fn, critical=critical)
            except Exception:
                logger.exception("Errore motion detection")
                with self.lock:
                    self.last_error = "Errore motion detection"

            time.sleep(self.config["min_interval"])

    def get_status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.config["enabled"],
                "motion_detected": self.motion_detected,
                "last_motion_at": self.last_motion_at_display,
                "current_area": round(self.current_area, 2),
                "last_trigger_area": round(self.last_trigger_area, 2),
                "threshold": self.config["threshold"],
                "min_area": self.config["min_area"],
                "trigger_frames": self.config["trigger_frames"],
                "save_frames": self.config["save_frames"],
                "last_capture_path": self.last_capture_path,
                "last_event_id": self.last_event_id,
                "last_preview_url": "/latest_motion.jpg" if self.last_capture_path else None,
                "error": self.last_error,
            }

    def apply_runtime_config(self, updates: dict) -> None:
        with self.lock:
            for key, value in updates.items():
                if key == "MOTION_ENABLED":
                    self.config["enabled"] = bool(value)
                    if self.config["enabled"]:
                        self.last_error = ""
                    else:
                        self.last_error = "Rilevamento movimento disabilitato"
                        self.motion_detected = False
                        self.trigger_streak = 0
                        self.clear_streak = 0
                elif key == "MOTION_MIN_AREA":
                    self.config["min_area"] = int(value)
                elif key == "MOTION_THRESHOLD":
                    self.config["threshold"] = int(value)
                    self.config["mog2_var_threshold"] = int(value)
                elif key == "MOTION_BLUR_SIZE":
                    self.config["blur_size"] = int(value)
                elif key == "MOTION_COOLDOWN":
                    self.config["cooldown"] = float(value)
                elif key == "MOTION_FRAME_INTERVAL":
                    self.config["min_interval"] = float(value)
                elif key == "MOTION_CAPTURE_INTERVAL":
                    self.config["capture_interval"] = float(value)
                elif key == "MOTION_MAX_AREA_RATIO":
                    self.config["max_area_ratio"] = float(value)
                elif key == "MOTION_WARMUP_FRAMES":
                    self.config["warmup_frames"] = int(value)
                elif key == "MOTION_TRIGGER_FRAMES":
                    self.config["trigger_frames"] = int(value)
                elif key == "MOTION_CLEAR_FRAMES":
                    self.config["clear_frames"] = int(value)
                elif key == "MOTION_BACKGROUND_ALPHA":
                    # Deprecated: legacy running-average alpha, no longer used by MOG2.
                    self.config["background_alpha"] = float(value)
                elif key == "MOTION_MOG2_HISTORY":
                    self.config["mog2_history"] = int(value)
                elif key == "MOTION_SCALE_WIDTH":
                    self.config["scale_width"] = int(value)
                elif key == "MOTION_MORPH_KERNEL":
                    self.config["morph_kernel"] = int(value)
                elif key == "MOTION_MORPH_DILATE_ITER":
                    self.config["morph_dilate_iter"] = int(value)
                elif key == "MOTION_GLOBAL_CHANGE_RATIO":
                    self.config["global_change_ratio"] = float(value)
                elif key == "MOTION_LEARNING_RATE":
                    self.config["learning_rate"] = float(value)
                elif key == "MOTION_LEARNING_RATE_ACTIVE":
                    self.config["learning_rate_active"] = float(value)
                elif key == "MOTION_SAVE_FRAMES":
                    self.config["save_frames"] = bool(value)
                elif key == "MOTION_SAVE_DIR":
                    self.config["save_dir"] = str(value)
                    self.event_store = MotionEventStore(self.config)
                    self.last_capture_path = None
                    self.last_event_id = None
                    self.last_motion_at = None
                    self.last_motion_at_display = None
                    self.last_capture_saved_at = None
                    self._classified_events.clear()
                    self._notified_events.clear()
                    self._restore_last_capture()
                elif key == "MOTION_EVENT_GAP":
                    self.config["event_gap"] = float(value)
                elif key == "MOTION_RETENTION_DAYS":
                    self.config["retention_days"] = float(value)
                elif key == "MOTION_RETENTION_MAX_MB":
                    self.config["retention_max_mb"] = float(value)
                elif key == "RECORD_ENABLED":
                    self.config["record_enabled"] = bool(value)
                elif key == "RECORD_FPS":
                    self.config["record_fps"] = float(value)
                elif key == "RECORD_PREROLL_SEC":
                    self.config["record_preroll_sec"] = float(value)
                elif key == "RECORD_POSTROLL_SEC":
                    self.config["record_postroll_sec"] = float(value)
                elif key == "RECORD_MAX_DURATION_SEC":
                    self.config["record_max_duration_sec"] = float(value)
                elif key == "RECORD_MAX_WIDTH":
                    self.config["record_max_width"] = int(value)
                elif key == "CONTINUOUS_RECORD_ENABLED":
                    self.config["continuous_record_enabled"] = bool(value)
                elif key == "CONTINUOUS_RECORD_SEGMENT_MIN":
                    self.config["continuous_record_segment_min"] = float(value)
                elif key == "CONTINUOUS_RECORD_RETAIN_HOURS":
                    self.config["continuous_record_retain_hours"] = float(value)
                elif key == "NOTIFY_PREFER_VIDEO":
                    self.config["notify_prefer_video"] = bool(value)
                elif key == "CLASSIFICATION_ENABLED":
                    self.config["classification_enabled"] = bool(value)
                elif key == "CLASSIFICATION_BACKEND":
                    self.config["classification_backend"] = str(value).strip().lower()
                elif key == "CLASSIFICATION_MIN_CONFIDENCE":
                    self.config["classification_min_confidence"] = float(value)
                elif key == "CLASSIFICATION_SAMPLE_POLICY":
                    self.config["classification_sample_policy"] = str(value).strip().lower()
                elif key == "CLASSIFICATION_LOCAL_MODEL_PATH":
                    self.config["classification_local_model_path"] = str(value).strip()
                elif key == "CLASSIFICATION_LOCAL_LABELS_PATH":
                    self.config["classification_local_labels_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_MODEL_PATH":
                    self.config["classification_detection_model_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_CONFIG_PATH":
                    self.config["classification_detection_config_path"] = str(value).strip()
                elif key == "CLASSIFICATION_DETECTION_INPUT_SIZE":
                    self.config["classification_detection_input_size"] = int(value)
                elif key == "CLASSIFICATION_YOLOX_MODEL_PATH":
                    self.config["classification_yolox_model_path"] = str(value).strip()
                elif key == "CLASSIFICATION_YOLOX_INPUT_SIZE":
                    self.config["classification_yolox_input_size"] = int(value)
                elif key == "CLASSIFICATION_PERSON_MIN_CONFIDENCE":
                    self.config["classification_person_min_confidence"] = float(value)
                elif key == "CLASSIFICATION_PET_MIN_CONFIDENCE":
                    self.config["classification_pet_min_confidence"] = float(value)
                elif key == "CLASSIFICATION_CROP_TO_MOTION":
                    self.config["classification_crop_to_motion"] = bool(value)
                elif key == "CLASSIFICATION_CROP_PADDING":
                    self.config["classification_crop_padding"] = float(value)
                elif key == "CLASSIFICATION_DETECT_PERSON":
                    self.config["classification_detect_person"] = bool(value)
                elif key == "CLASSIFICATION_DETECT_PET":
                    self.config["classification_detect_pet"] = bool(value)

            classifier_changed = any(
                key
                in {
                    "CLASSIFICATION_ENABLED",
                    "CLASSIFICATION_BACKEND",
                    "CLASSIFICATION_MIN_CONFIDENCE",
                    "CLASSIFICATION_SAMPLE_POLICY",
                    "CLASSIFICATION_LOCAL_MODEL_PATH",
                    "CLASSIFICATION_LOCAL_LABELS_PATH",
                    "CLASSIFICATION_DETECTION_MODEL_PATH",
                    "CLASSIFICATION_DETECTION_CONFIG_PATH",
                    "CLASSIFICATION_DETECTION_INPUT_SIZE",
                    "CLASSIFICATION_YOLOX_MODEL_PATH",
                    "CLASSIFICATION_YOLOX_INPUT_SIZE",
                    "CLASSIFICATION_PERSON_MIN_CONFIDENCE",
                    "CLASSIFICATION_PET_MIN_CONFIDENCE",
                    "CLASSIFICATION_DETECT_PERSON",
                    "CLASSIFICATION_DETECT_PET",
                }
                for key in updates
            )
            if classifier_changed:
                self.classifier = PersonPetClassifier.from_config(self.config)
                self._classified_events.clear()
                self._warn_if_classification_unready()

            reset_background = any(
                key
                in {
                    "MOTION_THRESHOLD",
                    "MOTION_BLUR_SIZE",
                    "MOTION_MIN_AREA",
                    "MOTION_FRAME_INTERVAL",
                    "MOTION_MOG2_HISTORY",
                    "MOTION_SCALE_WIDTH",
                    "MOTION_MORPH_KERNEL",
                }
                for key in updates
            )
            if reset_background:
                # Defer rebuild to the _run thread: MOG2 is stateful and applied there.
                self._needs_subtractor_rebuild = True
                self.processed_frames = 0
                self.trigger_streak = 0
                self.clear_streak = 0
                self.last_capture_saved_at = None

    def mark_false_alarm(self, event_id: str) -> bool:
        return self._get_event_store().mark_false_alarm(event_id)

    def list_events(self, limit: int = 8, include_frames: bool = False) -> list[dict]:
        return self._get_event_store().list_events(
            limit=limit,
            include_frames=include_frames,
        )

    def get_event(self, event_id: str):
        return self._get_event_store().get_event(event_id)

    def clear_events(self) -> int:
        with self.lock:
            removed = self._get_event_store().clear_all()
            self.last_capture_path = None
            self.last_event_id = None
            self.last_motion_at = None
            self.last_motion_at_display = None
            self.last_trigger_area = 0.0
            self.current_area = 0.0
            self.last_capture_saved_at = None
            self.motion_detected = False
            self._classified_events.clear()
            self._notified_events.clear()
            self._finalized_events.clear()
            self._automation_fired.clear()
            self._live_classify_attempts.clear()
            self._last_classified_notify_at = None
            self._last_notify_at = None
            return removed

    def purge_old_events(self) -> int:
        days = float(self.config.get("retention_days", 0) or 0)
        max_mb = float(self.config.get("retention_max_mb", 0) or 0)
        if days <= 0 and max_mb <= 0:
            return 0
        return self._get_event_store().purge_old_events(days, max_mb)
