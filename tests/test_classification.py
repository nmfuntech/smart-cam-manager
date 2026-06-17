import unittest

import numpy as np

from classification import (
    ClassificationResult,
    MobileNetSsdDetectorBackend,
    PersonPetClassifier,
)


class _FakeNet:
    """Stand-in for a cv2.dnn net returning a fixed [1, 1, N, 7] detection blob."""

    def __init__(self, detections):
        # detections: list of (class_id, confidence)
        rows = [[0.0, float(cid), float(conf), 0.1, 0.1, 0.5, 0.5] for cid, conf in detections]
        self._output = np.array([[rows]], dtype=np.float32) if rows else np.zeros((1, 1, 0, 7))

    def setInput(self, blob):
        self._blob = blob

    def forward(self):
        return self._output


def _detector_with(detections, min_score=0.5):
    backend = MobileNetSsdDetectorBackend(
        model_path="models/x.pb", config_path="models/x.pbtxt", min_score=min_score
    )
    backend.net = _FakeNet(detections)
    backend._loaded = True
    return backend


class FakeBackend:
    def __init__(self, result):
        self.result = result

    def classify(self, frame):
        return self.result

    def is_ready(self):
        return True


class PersonPetClassifierTests(unittest.TestCase):
    def test_maps_person_label_to_persona(self):
        classifier = PersonPetClassifier(
            enabled=True,
            backend_name="local",
            min_confidence=0.5,
            sample_policy="event_cover",
            backend=FakeBackend(
                ClassificationResult(
                    label="person",
                    confidence=0.91,
                    model_name="unit-model",
                    inference_ms=11.2,
                )
            ),
        )

        result = classifier.classify(np.zeros((32, 32, 3), dtype=np.uint8))

        self.assertEqual(result["class_label"], "persona")
        self.assertEqual(result["classification_status"], "ok")

    def test_maps_dog_to_pet(self):
        classifier = PersonPetClassifier(
            enabled=True,
            backend_name="local",
            min_confidence=0.5,
            sample_policy="event_cover",
            backend=FakeBackend(
                ClassificationResult(
                    label="dog",
                    confidence=0.87,
                    model_name="unit-model",
                    inference_ms=9.3,
                )
            ),
        )

        result = classifier.classify(np.zeros((32, 32, 3), dtype=np.uint8))

        self.assertEqual(result["class_label"], "animale_domestico")
        self.assertEqual(result["classification_status"], "ok")

    def test_low_confidence_goes_unknown(self):
        classifier = PersonPetClassifier(
            enabled=True,
            backend_name="local",
            min_confidence=0.95,
            sample_policy="event_cover",
            backend=FakeBackend(
                ClassificationResult(
                    label="person",
                    confidence=0.72,
                    model_name="unit-model",
                    inference_ms=7.8,
                )
            ),
        )

        result = classifier.classify(np.zeros((32, 32, 3), dtype=np.uint8))

        self.assertEqual(result["class_label"], "unknown")
        self.assertEqual(result["classification_status"], "low_confidence")

    def test_disabled_classifier_returns_none(self):
        classifier = PersonPetClassifier(
            enabled=False,
            backend_name="local",
            min_confidence=0.5,
            sample_policy="event_cover",
            backend=FakeBackend(None),
        )

        result = classifier.classify(np.zeros((32, 32, 3), dtype=np.uint8))

        self.assertIsNone(result)


class ClassifierReadinessTests(unittest.TestCase):
    def test_local_backend_not_ready_without_model_files(self):
        classifier = PersonPetClassifier.from_config(
            {
                "classification_enabled": True,
                "classification_backend": "local",
                "classification_local_model_path": "models/does_not_exist.onnx",
                "classification_local_labels_path": "models/does_not_exist.txt",
            }
        )
        self.assertTrue(classifier.enabled)
        self.assertFalse(classifier.ready)

    def test_cloud_backend_ready_with_http_endpoint(self):
        classifier = PersonPetClassifier.from_config(
            {
                "classification_enabled": True,
                "classification_backend": "cloud",
                "classification_cloud_endpoint": "https://example.test/classify",
            }
        )
        self.assertTrue(classifier.ready)

    def test_disabled_classifier_is_not_ready(self):
        classifier = PersonPetClassifier.from_config(
            {"classification_enabled": False, "classification_backend": "local"}
        )
        self.assertFalse(classifier.ready)


class _NoneBackend:
    def __init__(self, ready):
        self._ready = ready

    def classify(self, frame):
        return None

    def is_ready(self):
        return self._ready


class ClassifierNoneStatusTests(unittest.TestCase):
    def _classify(self, ready):
        classifier = PersonPetClassifier(
            enabled=True,
            backend_name="detection",
            min_confidence=0.5,
            sample_policy="event_cover",
            backend=_NoneBackend(ready=ready),
        )
        return classifier.classify(np.zeros((10, 10, 3), dtype=np.uint8))

    def test_ready_backend_with_no_result_is_no_detection(self):
        result = self._classify(ready=True)
        self.assertEqual(result["classification_status"], "no_detection")
        self.assertEqual(result["class_label"], "unknown")

    def test_unready_backend_is_unavailable(self):
        result = self._classify(ready=False)
        self.assertEqual(result["classification_status"], "unavailable")
        self.assertEqual(result["class_label"], "unknown")


class DetectionBackendTests(unittest.TestCase):
    def _frame(self):
        return np.zeros((480, 640, 3), dtype=np.uint8)

    def test_person_detection_maps_to_persona(self):
        backend = _detector_with([(1, 0.92)])
        classifier = PersonPetClassifier(
            enabled=True,
            backend_name="detection",
            min_confidence=0.5,
            sample_policy="event_cover",
            backend=backend,
        )
        result = classifier.classify(self._frame())
        self.assertEqual(result["class_label"], "persona")
        self.assertEqual(result["raw_label"], "person")
        self.assertEqual(result["classification_status"], "ok")

    def test_dog_detection_maps_to_pet(self):
        backend = _detector_with([(18, 0.81)])
        result = backend.classify(self._frame())
        self.assertEqual(result.label, "dog")

    def test_picks_highest_confidence_relevant_class(self):
        # cat=17 @ 0.6 and person=1 @ 0.88 -> person wins.
        backend = _detector_with([(17, 0.6), (1, 0.88)])
        result = backend.classify(self._frame())
        self.assertEqual(result.label, "person")
        self.assertAlmostEqual(result.confidence, 0.88, places=5)

    def test_ignores_non_pet_classes(self):
        # 3 is "car" in the COCO map; not person/cat/dog -> no relevant detection.
        backend = _detector_with([(3, 0.99)])
        self.assertIsNone(backend.classify(self._frame()))

    def test_below_min_score_returns_none(self):
        backend = _detector_with([(1, 0.30)], min_score=0.5)
        self.assertIsNone(backend.classify(self._frame()))

    def test_is_ready_false_without_model_files(self):
        backend = MobileNetSsdDetectorBackend(
            model_path="models/missing.pb", config_path="models/missing.pbtxt"
        )
        self.assertFalse(backend.is_ready())


class CategoryTargetTests(unittest.TestCase):
    def _classifier(self, *, person, pet):
        return PersonPetClassifier.from_config(
            {
                "classification_enabled": True,
                "classification_backend": "detection",
                "classification_min_confidence": 0.4,
                "classification_detect_person": person,
                "classification_detect_pet": pet,
            }
        )

    def _result(self, classifier, raw_label, confidence=0.9):
        classifier.backend = FakeBackend(
            ClassificationResult(
                label=raw_label, confidence=confidence, model_name="m", inference_ms=1.0
            )
        )
        return classifier.classify(np.zeros((10, 10, 3), dtype=np.uint8))

    def test_defaults_target_both_categories(self):
        classifier = PersonPetClassifier.from_config(
            {"classification_enabled": True, "classification_backend": "detection"}
        )
        self.assertEqual(classifier.targets, {"persona", "animale_domestico"})

    def test_person_only_ignores_pet(self):
        classifier = self._classifier(person=True, pet=False)
        person = self._result(classifier, "person")
        pet = self._result(classifier, "dog")
        self.assertEqual(person["classification_status"], "ok")
        self.assertEqual(person["class_label"], "persona")
        self.assertEqual(pet["classification_status"], "ignored")
        self.assertEqual(pet["class_label"], "unknown")
        # raw detection is preserved even when ignored.
        self.assertEqual(pet["raw_label"], "dog")
        # detected_label keeps the true category for archive/filter, even when ignored.
        self.assertEqual(person["detected_label"], "persona")
        self.assertEqual(pet["detected_label"], "animale_domestico")

    def test_detected_label_none_when_low_confidence(self):
        classifier = self._classifier(person=True, pet=True)
        result = self._result(classifier, "person", confidence=0.1)
        self.assertEqual(result["classification_status"], "low_confidence")
        self.assertIsNone(result["detected_label"])

    def test_pet_only_ignores_person(self):
        classifier = self._classifier(person=False, pet=True)
        self.assertEqual(self._result(classifier, "cat")["classification_status"], "ok")
        self.assertEqual(self._result(classifier, "person")["classification_status"], "ignored")

    def test_both_disabled_ignores_everything(self):
        classifier = self._classifier(person=False, pet=False)
        self.assertEqual(classifier.targets, set())
        self.assertEqual(self._result(classifier, "person")["classification_status"], "ignored")
        self.assertEqual(self._result(classifier, "dog")["classification_status"], "ignored")


if __name__ == "__main__":
    unittest.main()
