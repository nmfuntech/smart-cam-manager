import unittest

import numpy as np

from classification import ClassificationResult, PersonPetClassifier


class FakeBackend:
    def __init__(self, result):
        self.result = result

    def classify(self, frame):
        return self.result


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


if __name__ == "__main__":
    unittest.main()
