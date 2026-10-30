import ipaddress
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    label: str
    confidence: float
    model_name: str
    inference_ms: float


class ClassifierBackend(Protocol):
    def classify(self, frame: np.ndarray) -> ClassificationResult | None: ...

    def is_ready(self) -> bool:
        """Whether the backend can actually run (model/labels present or endpoint set)."""
        ...


class OpenCvDnnClassifierBackend:
    """Single-image classifier via OpenCV DNN and ONNX/Caffe style models."""

    def __init__(
        self,
        model_path: str,
        labels_path: str,
        input_width: int = 224,
        input_height: int = 224,
        scale: float = 1.0 / 255.0,
        swap_rb: bool = True,
    ):
        self.model_path = model_path
        self.labels_path = labels_path
        self.input_width = input_width
        self.input_height = input_height
        self.scale = scale
        self.swap_rb = swap_rb
        self.model_name = f"opencv-dnn:{Path(model_path).name}"
        self.labels: list[str] = []
        self.net = None
        self._loaded = False

    def _load_labels(self, labels_path: str) -> list[str]:
        path = Path(labels_path)
        if not path.exists():
            logger.warning("Classification labels file not found: %s", labels_path)
            return []
        labels = []
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value:
                labels.append(value)
        return labels

    def _load_network(self, model_path: str):
        path = Path(model_path)
        if not path.exists():
            logger.warning("Classification model not found: %s", model_path)
            return None
        try:
            return cv2.dnn.readNet(str(path))
        except Exception:
            logger.exception("Unable to load classification model: %s", model_path)
            return None

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        self._ensure_loaded()
        if self.net is None or not self.labels:
            return None
        start = time.perf_counter()
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=self.scale,
            size=(self.input_width, self.input_height),
            mean=(0.0, 0.0, 0.0),
            swapRB=self.swap_rb,
            crop=True,
        )
        self.net.setInput(blob)
        output = self.net.forward()
        probabilities = np.array(output).reshape(-1)
        if probabilities.size == 0:
            return None
        top_index = int(np.argmax(probabilities))
        if top_index >= len(self.labels):
            return None
        confidence = float(probabilities[top_index])
        label = self.labels[top_index]
        inference_ms = (time.perf_counter() - start) * 1000.0
        return ClassificationResult(
            label=label,
            confidence=confidence,
            model_name=self.model_name,
            inference_ms=round(inference_ms, 2),
        )

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.labels = self._load_labels(self.labels_path)
        self.net = self._load_network(self.model_path)
        self._loaded = True

    def is_ready(self) -> bool:
        return Path(self.model_path).exists() and Path(self.labels_path).exists()


class YoloxDetectorBackend:
    """Object detection with YOLOX (Apache-2.0) ONNX models through OpenCV DNN.

    Replaces MobileNet-SSD v2 (2018) as the default detector: on real events of
    this installation SSD labelled half-visible people as "cat" and missed the
    dog in IR night frames. YOLOX-Tiny is far more accurate at a similar cost on
    the mini PC CPU, YOLOX-Nano is the low-end fallback. No new dependency:
    OpenCV's DNN module runs the ONNX graph.

    The exported graph returns raw head outputs ``[1, N, 85]`` (cx, cy, w, h on
    the stride grid, objectness, 80 class scores): the grid decode and NMS
    happen here. COCO-80 indices: person=0, cat=15, dog=16.
    """

    COCO_LABELS = {0: "person", 15: "cat", 16: "dog"}
    STRIDES = (8, 16, 32)

    def __init__(
        self,
        model_path: str,
        input_size: int = 416,
        min_score: float = 0.45,
        nms_threshold: float = 0.45,
        num_threads: int = 0,
    ):
        self.model_path = model_path
        self.input_size = int(input_size)
        self.min_score = float(min_score)
        self.nms_threshold = float(nms_threshold)
        self.num_threads = int(num_threads)
        self.model_name = f"yolox:{Path(model_path).name}"
        self.net = None
        self._loaded = False
        self._grids = None
        self._strides = None
        self.last_detections: list[tuple[str, float, tuple[float, float, float, float]]] = []

    def is_ready(self) -> bool:
        return Path(self.model_path).exists()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.is_ready():
            logger.warning("Modello YOLOX non trovato: %s", self.model_path)
            return
        try:
            self.net = cv2.dnn.readNetFromONNX(self.model_path)
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        except Exception:
            logger.exception("Impossibile caricare il modello YOLOX: %s", self.model_path)
            self.net = None
            return
        grids, strides = [], []
        for stride in self.STRIDES:
            size = self.input_size // stride
            ys, xs = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
            grids.append(np.stack((xs, ys), axis=-1).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        self._grids = np.concatenate(grids).astype(np.float32)
        self._strides = np.concatenate(strides).astype(np.float32)

    def _letterbox(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        height, width = frame.shape[:2]
        ratio = min(self.input_size / height, self.input_size / width)
        resized = cv2.resize(
            frame,
            (int(width * ratio), int(height * ratio)),
            interpolation=cv2.INTER_LINEAR,
        )
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        return padded, ratio

    def detect(
        self, frame: np.ndarray
    ) -> list[tuple[str, float, tuple[float, float, float, float]]]:
        """Person/cat/dog detections as (label, score, (x, y, w, h)) in frame pixels."""
        self._ensure_loaded()
        if self.net is None or frame is None or frame.size == 0:
            return []
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        padded, ratio = self._letterbox(frame)
        blob = cv2.dnn.blobFromImage(padded, 1.0, swapRB=False, crop=False)
        if self.num_threads > 0:
            cv2.setNumThreads(self.num_threads)
        self.net.setInput(blob)
        output = np.array(self.net.forward())[0]
        if output.shape[0] != self._grids.shape[0]:
            logger.warning("Output YOLOX inatteso: %s", output.shape)
            return []
        boxes_xy = (output[:, :2] + self._grids) * self._strides
        boxes_wh = np.exp(np.clip(output[:, 2:4], -10, 10)) * self._strides
        objectness = output[:, 4:5]
        wanted = list(self.COCO_LABELS)
        scores = objectness * output[:, 5:][:, wanted]

        detections = []
        for column, class_id in enumerate(wanted):
            class_scores = scores[:, column]
            keep = np.where(class_scores >= self.min_score)[0]
            if keep.size == 0:
                continue
            rects = []
            for index in keep:
                cx, cy = boxes_xy[index]
                w, h = boxes_wh[index]
                rects.append(
                    [float((cx - w / 2) / ratio), float((cy - h / 2) / ratio),
                     float(w / ratio), float(h / ratio)]
                )
            selected = cv2.dnn.NMSBoxes(
                rects, class_scores[keep].tolist(), self.min_score, self.nms_threshold
            )
            kept_scores = class_scores[keep]
            label = self.COCO_LABELS[class_id]
            for pick in np.array(selected).reshape(-1):
                detections.append((label, float(kept_scores[pick]), tuple(rects[pick])))
        detections.sort(key=lambda item: item[1], reverse=True)
        return detections

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        start = time.perf_counter()
        detections = self.detect(frame)
        inference_ms = (time.perf_counter() - start) * 1000.0
        self.last_detections = detections
        if not detections:
            return None
        # Security first: if a person is confidently in the frame, the event is
        # about a person even when the dog scores higher (owner walking the dog).
        people = [d for d in detections if d[0] == "person"]
        best = people[0] if people else detections[0]
        return ClassificationResult(
            label=best[0],
            confidence=best[1],
            model_name=self.model_name,
            inference_ms=round(inference_ms, 2),
        )


class UnsupportedBackend:
    def __init__(self, backend_name: str):
        self.backend_name = backend_name

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        return None

    def is_ready(self) -> bool:
        return False


class TeachableMachineBackend(OpenCvDnnClassifierBackend):
    """OpenCV DNN backend for ONNX models exported from Google Teachable Machine.

    Teachable Machine normalizes input to [-1, 1]: pixel = (pixel / 127.5) - 1.0.
    """

    def __init__(
        self,
        model_path: str,
        labels_path: str,
        input_width: int = 224,
        input_height: int = 224,
    ):
        super().__init__(
            model_path=model_path,
            labels_path=labels_path,
            input_width=input_width,
            input_height=input_height,
            scale=1.0 / 127.5,
            swap_rb=True,
        )
        self.model_name = f"teachable-machine:{Path(model_path).name}"

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        self._ensure_loaded()
        if self.net is None or not self.labels:
            return None
        start = time.perf_counter()
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=self.scale,
            size=(self.input_width, self.input_height),
            mean=(127.5, 127.5, 127.5),
            swapRB=self.swap_rb,
            crop=True,
        )
        self.net.setInput(blob)
        output = self.net.forward()
        probabilities = np.array(output).reshape(-1)
        if probabilities.size == 0:
            return None
        top_index = int(np.argmax(probabilities))
        if top_index >= len(self.labels):
            return None
        confidence = float(probabilities[top_index])
        label = self.labels[top_index]
        inference_ms = (time.perf_counter() - start) * 1000.0
        return ClassificationResult(
            label=label,
            confidence=confidence,
            model_name=self.model_name,
            inference_ms=round(inference_ms, 2),
        )


class MobileNetSsdDetectorBackend:
    """Object detection via OpenCV DNN + MobileNet-SSD v2 trained on COCO.

    Runs once per motion event, so accuracy matters more than raw speed. Unlike a
    whole-frame classifier, it localizes the subject and recognizes the COCO
    classes we care about (person/cat/dog) without any custom training. We return
    the raw COCO label and let PersonPetClassifier.LABEL_MAP normalize it.

    COCO 90-class ids (TensorFlow SSD label map): person=1, cat=17, dog=18.
    Network output is shape [1, 1, N, 7] with rows
    [_, classId, confidence, x1, y1, x2, y2] in normalized [0, 1] coordinates.
    """

    # COCO class id -> raw label understood by PersonPetClassifier.LABEL_MAP.
    COCO_LABELS = {1: "person", 17: "cat", 18: "dog"}
    # Se person e pet sono entrambi presenti, preferisci il pet quando la persona
    # non supera il pet di almeno questo margine (evita cane → persona).
    DEFAULT_PERSON_OVER_PET_MARGIN = 0.12

    def __init__(
        self,
        model_path: str,
        config_path: str,
        input_size: int = 300,
        min_score: float = 0.5,
        pet_priority_margin: float | None = None,
    ):
        self.model_path = model_path
        self.config_path = config_path
        self.input_size = int(input_size)
        self.min_score = float(min_score)
        try:
            env_margin = float(os.getenv("CLASSIFICATION_PET_PRIORITY_MARGIN", ""))
        except ValueError:
            env_margin = self.DEFAULT_PERSON_OVER_PET_MARGIN
        self.pet_priority_margin = (
            float(pet_priority_margin) if pet_priority_margin is not None else env_margin
        )
        self.model_name = f"mobilenet-ssd:{Path(model_path).name}"
        self.net = None
        self._loaded = False

    def is_ready(self) -> bool:
        return Path(self.model_path).exists() and Path(self.config_path).exists()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.is_ready():
            logger.warning(
                "Modello detection non trovato: %s / %s", self.model_path, self.config_path
            )
            return
        try:
            self.net = cv2.dnn.readNetFromTensorflow(self.model_path, self.config_path)
        except Exception:
            logger.exception("Impossibile caricare il modello detection: %s", self.model_path)
            self.net = None

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        self._ensure_loaded()
        if self.net is None:
            return None
        start = time.perf_counter()
        blob = cv2.dnn.blobFromImage(
            frame,
            scalefactor=1.0,
            size=(self.input_size, self.input_size),
            mean=(0.0, 0.0, 0.0),
            swapRB=True,
            crop=False,
        )
        self.net.setInput(blob)
        output = np.array(self.net.forward()).reshape(-1, 7)
        inference_ms = (time.perf_counter() - start) * 1000.0

        candidates: dict[str, float] = {}
        for row in output:
            class_id = int(row[1])
            confidence = float(row[2])
            label = self.COCO_LABELS.get(class_id)
            if label is None or confidence < self.min_score:
                continue
            if confidence > candidates.get(label, 0.0):
                candidates[label] = confidence

        if not candidates:
            return None

        person_score = candidates.get("person", 0.0)
        pet_labels = [label for label in ("cat", "dog") if label in candidates]
        best_pet_label = (
            max(pet_labels, key=lambda label: candidates[label]) if pet_labels else None
        )
        best_pet_score = candidates[best_pet_label] if best_pet_label else 0.0

        if best_pet_label and person_score > 0.0:
            if person_score - best_pet_score < self.pet_priority_margin:
                best_label = best_pet_label
                best_score = best_pet_score
            else:
                best_label = "person"
                best_score = person_score
        elif person_score > 0.0:
            best_label = "person"
            best_score = person_score
        elif best_pet_label:
            best_label = best_pet_label
            best_score = best_pet_score
        else:
            return None

        return ClassificationResult(
            label=best_label,
            confidence=best_score,
            model_name=self.model_name,
            inference_ms=round(inference_ms, 2),
        )


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True per gli indirizzi che un classificatore legittimo non userebbe mai.

    Blocca link-local (169.254.0.0/16 incl. 169.254.169.254, e fe80::/10) — il
    classico bersaglio dei metadata cloud — più gli indirizzi non instradabili
    (unspecified/multicast). Le reti private RFC1918 e il loopback restano
    ammessi: un classificatore self-hosted in LAN o su localhost è un uso valido.
    """
    # Normalizza l'eventuale forma IPv4-mapped (::ffff:a.b.c.d) all'IPv4 reale,
    # così un bypass via mapping non aggira il controllo link-local.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_link_local or addr.is_unspecified or addr.is_multicast


def _endpoint_targets_metadata(endpoint: str) -> bool:
    """True se l'endpoint va bloccato come potenziale bersaglio SSRF.

    Fail-closed: se l'host non è presente o non è risolvibile, blocca (meglio
    saltare una classificazione che lasciare aperta una richiesta verso un host
    non verificabile). Controlla TUTTI gli indirizzi risolti: se anche uno solo è
    bloccato (es. un nome che risolve a 169.254.169.254 via DNS rebinding), nega.
    """
    host = urlparse(endpoint).hostname
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Fail-closed: host non risolvibile -> non procedere.
        return True
    saw_addr = False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        saw_addr = True
        if _is_blocked_address(addr):
            return True
    # Nessun indirizzo valido estratto -> fail-closed.
    return not saw_addr


class CloudBackend:
    """HTTP REST endpoint backend. POST JPEG → JSON {label, confidence}."""

    def __init__(self, endpoint: str, api_key: str = ""):
        self.endpoint = endpoint.strip()
        self.api_key = api_key.strip()
        self.model_name = "cloud"

    def is_ready(self) -> bool:
        return self.endpoint.lower().startswith(("http://", "https://"))

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        if not self.endpoint:
            logger.warning("CLASSIFICATION_CLOUD_ENDPOINT non configurato")
            return None
        # Defense in depth against SSRF: only ever speak HTTP(S). Rejects file://,
        # gopher://, ftp:// and similar schemes if the endpoint is ever misconfigured.
        if not self.endpoint.lower().startswith(("http://", "https://")):
            logger.warning("CLASSIFICATION_CLOUD_ENDPOINT deve usare http(s)://")
            return None
        if _endpoint_targets_metadata(self.endpoint):
            logger.warning(
                "CLASSIFICATION_CLOUD_ENDPOINT punta a un indirizzo link-local/metadata: bloccato"
            )
            return None
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        req = urllib.request.Request(self.endpoint, data=buf.tobytes(), method="POST")
        req.add_header("Content-Type", "image/jpeg")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            inference_ms = (time.perf_counter() - start) * 1000.0
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            logger.warning("Cloud classification failed: %s", exc)
            return None
        label = str(body.get("label", "")).strip()
        confidence = float(body.get("confidence", 0.0))
        if not label:
            return None
        return ClassificationResult(
            label=label,
            confidence=confidence,
            model_name=self.model_name,
            inference_ms=round(inference_ms, 2),
        )


class PersonPetClassifier:
    LABEL_PERSONA = "persona"
    LABEL_PET = "animale_domestico"
    LABEL_UNKNOWN = "unknown"

    LABEL_MAP = {
        "person": LABEL_PERSONA,
        "persona": LABEL_PERSONA,
        "human": LABEL_PERSONA,
        "dog": LABEL_PET,
        "cat": LABEL_PET,
        "pet": LABEL_PET,
        "animale_domestico": LABEL_PET,
    }

    def __init__(
        self,
        enabled: bool,
        backend_name: str,
        min_confidence: float,
        sample_policy: str,
        backend: ClassifierBackend,
        targets: set[str] | None = None,
    ):
        self.enabled = enabled
        self.backend_name = backend_name
        self.min_confidence = min_confidence
        self.sample_policy = sample_policy
        self.backend = backend
        # Categories the user actually wants to be alerted about. A detected class
        # outside this set is reported as "ignored" so callers can skip notifying.
        # Defaults to both for backwards compatibility.
        self.targets = targets if targets is not None else {self.LABEL_PERSONA, self.LABEL_PET}
        # Optional per-class thresholds (YOLOX); fall back to min_confidence.
        self.class_min_confidence: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        """True when enabled AND the backend has its model/labels (or cloud endpoint)."""
        return self.enabled and self.backend.is_ready()

    @classmethod
    def from_config(cls, config: dict) -> "PersonPetClassifier":
        enabled = bool(config.get("classification_enabled", False))
        backend_name = str(config.get("classification_backend", "local")).strip().lower()
        min_confidence = float(config.get("classification_min_confidence", 0.55))
        sample_policy = (
            str(config.get("classification_sample_policy", "event_cover")).strip().lower()
        )
        targets: set[str] = set()
        if bool(config.get("classification_detect_person", True)):
            targets.add(cls.LABEL_PERSONA)
        if bool(config.get("classification_detect_pet", True)):
            targets.add(cls.LABEL_PET)

        if not enabled:
            backend: ClassifierBackend = UnsupportedBackend("disabled")
        elif backend_name == "local":
            backend: ClassifierBackend = OpenCvDnnClassifierBackend(
                model_path=str(
                    config.get("classification_local_model_path", "models/person_pet.onnx")
                ),
                labels_path=str(
                    config.get("classification_local_labels_path", "models/person_pet_labels.txt")
                ),
                input_width=int(config.get("classification_local_input_width", 224)),
                input_height=int(config.get("classification_local_input_height", 224)),
                scale=float(config.get("classification_local_scale", 1.0 / 255.0)),
                swap_rb=bool(config.get("classification_local_swap_rb", True)),
            )
        elif backend_name == "teachable_machine":
            backend = TeachableMachineBackend(
                model_path=str(
                    config.get("classification_tm_model_path", "models/teachable_machine.onnx")
                ),
                labels_path=str(
                    config.get(
                        "classification_tm_labels_path", "models/teachable_machine_labels.txt"
                    )
                ),
                input_width=int(config.get("classification_tm_input_width", 224)),
                input_height=int(config.get("classification_tm_input_height", 224)),
            )
        elif backend_name == "yolox":
            backend = YoloxDetectorBackend(
                model_path=str(
                    config.get("classification_yolox_model_path", "models/yolox_tiny.onnx")
                ),
                input_size=int(config.get("classification_yolox_input_size", 416)),
                # The backend keeps anything above the lower of the two class
                # thresholds; the classifier then applies the per-class one.
                min_score=min(
                    float(config.get("classification_person_min_confidence", 0.35)),
                    float(config.get("classification_pet_min_confidence", 0.15)),
                ),
                num_threads=int(config.get("classification_num_threads", 0) or 0),
            )
        elif backend_name == "detection":
            backend = MobileNetSsdDetectorBackend(
                model_path=str(
                    config.get(
                        "classification_detection_model_path",
                        "models/ssd_mobilenet_v2_coco.pb",
                    )
                ),
                config_path=str(
                    config.get(
                        "classification_detection_config_path",
                        "models/ssd_mobilenet_v2_coco.pbtxt",
                    )
                ),
                input_size=int(config.get("classification_detection_input_size", 300)),
                min_score=min_confidence,
            )
        elif backend_name == "cloud":
            backend = CloudBackend(
                endpoint=str(config.get("classification_cloud_endpoint", "")),
                api_key=str(config.get("classification_cloud_api_key", "")),
            )
        else:
            backend = UnsupportedBackend("unknown")

        classifier = cls(
            enabled=enabled,
            backend_name=backend_name,
            min_confidence=min_confidence,
            sample_policy=sample_policy,
            backend=backend,
            targets=targets,
        )
        if backend_name == "yolox":
            # Measured on real events: people are unambiguous above 0.35, while
            # the dog in IR night frames often scores 0.15-0.35 (see
            # docs/rilevamento.md). One global threshold cannot serve both.
            classifier.class_min_confidence = {
                cls.LABEL_PERSONA: float(config.get("classification_person_min_confidence", 0.35)),
                cls.LABEL_PET: float(config.get("classification_pet_min_confidence", 0.15)),
            }
        return classifier

    def classify(self, frame: np.ndarray) -> dict | None:
        if not self.enabled:
            return None
        result = self.backend.classify(frame)
        if result is None:
            # Distinguish a missing/unusable model ("unavailable") from a model that
            # ran but found no person/pet in the frame ("no_detection").
            status = "no_detection" if self.backend.is_ready() else "unavailable"
            return {
                "class_label": self.LABEL_UNKNOWN,
                "confidence": None,
                "model_name": self.backend_name,
                "inference_ms": None,
                "backend": self.backend_name,
                "classification_status": status,
            }

        normalized = self._normalize_label(result.label)
        is_person_or_pet = normalized in {self.LABEL_PERSONA, self.LABEL_PET}
        threshold = self.class_min_confidence.get(normalized, self.min_confidence)
        confident = result.confidence >= threshold
        in_targets = normalized in self.targets
        accepted = is_person_or_pet and confident and in_targets

        if accepted:
            status = "ok"
        elif is_person_or_pet and not confident:
            status = "low_confidence"
        elif is_person_or_pet and not in_targets:
            # A person/pet was recognized, but this category is disabled by the user.
            status = "ignored"
        else:
            status = "unknown"

        # The recognized category regardless of whether we notify for it: lets the
        # archive label/filter a confident detection (incl. "ignored" ones) by category.
        detected_label = normalized if (is_person_or_pet and confident) else None

        return {
            "class_label": normalized if accepted else self.LABEL_UNKNOWN,
            "detected_label": detected_label,
            "confidence": round(float(result.confidence), 4),
            "raw_label": result.label,
            "model_name": result.model_name,
            "inference_ms": result.inference_ms,
            "backend": self.backend_name,
            "classification_status": status,
        }

    def _normalize_label(self, value: str) -> str:
        label = (value or "").strip().lower()
        return self.LABEL_MAP.get(label, self.LABEL_UNKNOWN)
