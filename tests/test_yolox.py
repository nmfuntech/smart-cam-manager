import tempfile
import unittest
from pathlib import Path

import numpy as np

from blackframe.classification import PersonPetClassifier, YoloxDetectorBackend


class _FakeNet:
    def __init__(self, output):
        self.output = output

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self.output


def _backend_with(anchors, min_score=0.15, size=416):
    """anchors: list of (index, class_id, score, raw_xywh)."""
    count = sum((size // s) ** 2 for s in (8, 16, 32))
    output = np.zeros((1, count, 85), dtype=np.float32)
    for index, class_id, score, raw in anchors:
        output[0, index, :4] = raw
        output[0, index, 4] = 1.0
        output[0, index, 5 + class_id] = score
    tmp = tempfile.NamedTemporaryFile(suffix=".onnx", delete=False)
    tmp.close()
    backend = YoloxDetectorBackend(tmp.name, input_size=size, min_score=min_score)
    backend._ensure_loaded = lambda: None
    backend._loaded = True
    backend.net = _FakeNet(output)
    # Same grid construction as the real loader.
    grids, strides = [], []
    for stride in backend.STRIDES:
        n = size // stride
        ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        grids.append(np.stack((xs, ys), axis=-1).reshape(-1, 2))
        strides.append(np.full((n * n, 1), stride))
    backend._grids = np.concatenate(grids).astype(np.float32)
    backend._strides = np.concatenate(strides).astype(np.float32)
    return backend


class YoloxDecodeTests(unittest.TestCase):
    def test_grid_decode_maps_back_to_frame_pixels(self):
        # Anchor (row 10, col 20) on the stride-8 grid; offsets 0.5 -> centre
        # (20.5*8, 10.5*8) = (164, 84) in the 416 letterbox; w=h=exp(0)*8=8.
        index = 10 * 52 + 20
        backend = _backend_with([(index, 0, 0.9, (0.5, 0.5, 0.0, 0.0))])
        frame = np.zeros((208, 416, 3), dtype=np.uint8)  # ratio 1.0, padded below
        label, score, (x, y, w, h) = backend.detect(frame)[0]
        self.assertEqual(label, "person")
        self.assertAlmostEqual(score, 0.9, places=4)
        self.assertAlmostEqual(x, 160.0, places=3)
        self.assertAlmostEqual(y, 80.0, places=3)
        self.assertAlmostEqual(w, 8.0, places=3)

    def test_letterbox_ratio_scales_boxes(self):
        index = 10 * 52 + 20
        backend = _backend_with([(index, 16, 0.8, (0.5, 0.5, 0.0, 0.0))])
        frame = np.zeros((416, 832, 3), dtype=np.uint8)  # ratio 0.5
        label, _, (x, y, w, _h) = backend.detect(frame)[0]
        self.assertEqual(label, "dog")
        self.assertAlmostEqual(x, 320.0, places=3)
        self.assertAlmostEqual(w, 16.0, places=3)

    def test_ignores_other_coco_classes_and_low_scores(self):
        backend = _backend_with([(5, 2, 0.99, (0, 0, 0, 0)), (6, 0, 0.05, (0, 0, 0, 0))])
        self.assertEqual(backend.detect(np.zeros((100, 100, 3), np.uint8)), [])

    def test_person_wins_over_higher_scoring_dog(self):
        backend = _backend_with(
            [(100, 16, 0.95, (0, 0, 1, 1)), (900, 0, 0.6, (0, 0, 1, 1))]
        )
        result = backend.classify(np.zeros((416, 416, 3), np.uint8))
        self.assertEqual(result.label, "person")
        self.assertEqual(len(backend.last_detections), 2)


class YoloxClassifierThresholdTests(unittest.TestCase):
    def _classifier(self, backend):
        config = {
            "classification_enabled": True,
            "classification_backend": "yolox",
            "classification_min_confidence": 0.5,
            "classification_person_min_confidence": 0.35,
            "classification_pet_min_confidence": 0.15,
            "classification_yolox_model_path": backend.model_path,
        }
        classifier = PersonPetClassifier.from_config(config)
        classifier.backend = backend
        return classifier

    def test_pet_accepted_below_global_threshold(self):
        backend = _backend_with([(100, 16, 0.2, (0, 0, 1, 1))])
        result = self._classifier(backend).classify(np.zeros((416, 416, 3), np.uint8))
        self.assertEqual(result["classification_status"], "ok")
        self.assertEqual(result["class_label"], "animale_domestico")

    def test_weak_person_is_low_confidence(self):
        backend = _backend_with([(100, 0, 0.3, (0, 0, 1, 1))])
        result = self._classifier(backend).classify(np.zeros((416, 416, 3), np.uint8))
        self.assertEqual(result["classification_status"], "low_confidence")

    def test_missing_model_reports_unavailable(self):
        config = {
            "classification_enabled": True,
            "classification_backend": "yolox",
            "classification_yolox_model_path": str(Path("/nope/yolox.onnx")),
        }
        classifier = PersonPetClassifier.from_config(config)
        self.assertFalse(classifier.ready)
        result = classifier.classify(np.zeros((32, 32, 3), np.uint8))
        self.assertEqual(result["classification_status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
