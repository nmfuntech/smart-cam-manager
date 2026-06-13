import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


class UnsupportedBackend:
    def __init__(self, backend_name: str):
        self.backend_name = backend_name

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        return None


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


class CloudBackend:
    """HTTP REST endpoint backend. POST JPEG → JSON {label, confidence}."""

    def __init__(self, endpoint: str, api_key: str = ""):
        self.endpoint = endpoint.strip()
        self.api_key = api_key.strip()
        self.model_name = "cloud"

    def classify(self, frame: np.ndarray) -> ClassificationResult | None:
        if not self.endpoint:
            logger.warning("CLASSIFICATION_CLOUD_ENDPOINT non configurato")
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
    ):
        self.enabled = enabled
        self.backend_name = backend_name
        self.min_confidence = min_confidence
        self.sample_policy = sample_policy
        self.backend = backend

    @classmethod
    def from_config(cls, config: dict) -> "PersonPetClassifier":
        enabled = bool(config.get("classification_enabled", False))
        backend_name = str(config.get("classification_backend", "local")).strip().lower()
        min_confidence = float(config.get("classification_min_confidence", 0.55))
        sample_policy = (
            str(config.get("classification_sample_policy", "event_cover")).strip().lower()
        )

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
        elif backend_name == "cloud":
            backend = CloudBackend(
                endpoint=str(config.get("classification_cloud_endpoint", "")),
                api_key=str(config.get("classification_cloud_api_key", "")),
            )
        else:
            backend = UnsupportedBackend("unknown")

        return cls(
            enabled=enabled,
            backend_name=backend_name,
            min_confidence=min_confidence,
            sample_policy=sample_policy,
            backend=backend,
        )

    def classify(self, frame: np.ndarray) -> dict | None:
        if not self.enabled:
            return None
        result = self.backend.classify(frame)
        if result is None:
            return {
                "class_label": self.LABEL_UNKNOWN,
                "confidence": None,
                "model_name": self.backend_name,
                "inference_ms": None,
                "backend": self.backend_name,
                "classification_status": "unavailable",
            }

        normalized = self._normalize_label(result.label)
        accepted = normalized in {self.LABEL_PERSONA, self.LABEL_PET}
        if result.confidence < self.min_confidence:
            accepted = False

        return {
            "class_label": normalized if accepted else self.LABEL_UNKNOWN,
            "confidence": round(float(result.confidence), 4),
            "raw_label": result.label,
            "model_name": result.model_name,
            "inference_ms": result.inference_ms,
            "backend": self.backend_name,
            "classification_status": "ok" if accepted else "low_confidence",
        }

    def _normalize_label(self, value: str) -> str:
        label = (value or "").strip().lower()
        return self.LABEL_MAP.get(label, self.LABEL_UNKNOWN)
