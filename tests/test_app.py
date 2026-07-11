import importlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from blackframe.runtime_config import RuntimeConfigManager


def load_app_module():
    env = {
        "APP_ADMIN_PASSWORD": "admin-pass",
        "APP_SECRET_KEY": "test-secret",
        "TAPO_USERNAME": "user",
        "TAPO_PASSWORD": "pass",
        "TAPO_HOST": "127.0.0.1",
    }
    restore_targets = [
        Path(".env"),
        Path("data/camera_profiles.json"),
        Path("data/.camera_profiles.key"),
    ]
    backups = {}
    for path in restore_targets:
        backups[path] = path.read_bytes() if path.exists() else None

    try:
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch("threading.Thread.start", lambda self: None):
                import blackframe.app as app

                return importlib.reload(app)
    finally:
        for path, content in backups.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)


def build_feature_services(app_module):
    return app_module.FeatureServices(
        presets=app_module.PresetService("data/test-presets.json"),
        notifications=app_module.NotificationService(),
        recording=app_module.RecordingService("captures/test-recordings"),
        camera_profiles=app_module.CameraProfileService("data/test-camera-profiles.json"),
        wifi=app_module.WifiService(),
    )


def authenticate_client(client, csrf_token: str = "test-csrf-token") -> str:
    with client.session_transaction() as session_state:
        session_state["blackframe_auth_user"] = "admin"
        session_state["blackframe_csrf_token"] = csrf_token
    return csrf_token


def csrf_headers(csrf_token: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token}


class MotionCropTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _detector(self, rect, crop=True, padding=0.0):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {
            "classification_crop_to_motion": crop,
            "classification_crop_padding": padding,
        }
        detector._last_motion_rect_norm = rect
        return detector

    def _frame(self):
        import numpy as np

        # 100x200 frame (h x w); fill with row/col gradient to check the cropped region.
        return np.zeros((100, 200, 3), dtype=np.uint8)

    def test_crops_to_normalized_rect_without_padding(self):
        detector = self._detector((0.25, 0.5, 0.25, 0.25), padding=0.0)
        crop = detector._crop_to_motion(self._frame())
        # x: 0.25*200=50 -> 0.5*200=100 (w=50); y: 0.5*100=50 -> 0.75*100=75 (h=25)
        self.assertEqual(crop.shape[:2], (25, 50))

    def test_padding_expands_and_clamps_to_borders(self):
        detector = self._detector((0.0, 0.0, 0.5, 0.5), padding=0.5)
        crop = detector._crop_to_motion(self._frame())
        # Padding cannot go below 0; right/bottom extend by 0.5*0.5=0.25 -> 0.75.
        self.assertEqual(crop.shape[:2], (75, 150))

    def test_returns_full_frame_when_disabled(self):
        detector = self._detector((0.25, 0.25, 0.25, 0.25), crop=False)
        frame = self._frame()
        self.assertIs(detector._crop_to_motion(frame), frame)

    def test_returns_full_frame_when_no_motion_rect(self):
        detector = self._detector(None)
        frame = self._frame()
        self.assertIs(detector._crop_to_motion(frame), frame)


class ClassificationNotifyGateTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _detector(self, status, *, prefer_video=False, detected_label=None, targets=None):
        class FakeStore:
            current_event_dir = None

            def event_has_classification(self, event_id):
                return False

            def save_event_meta(self, event_id, data):
                pass

            def rename_event_with_label(self, event_id, label):
                return f"{event_id}__{label}" if label else event_id

            def find_event_dir(self, event_id):
                return None

            def ensure_preview_image(self, event_id):
                return False

        class FakeClassifier:
            enabled = True
            sample_policy = "event_cover"
            LABEL_PERSONA = "persona"
            LABEL_PET = "animale_domestico"

            def __init__(self):
                self.targets = targets if targets is not None else {"persona", "animale_domestico"}

            def classify(self, frame):
                return {
                    "class_label": "persona",
                    "classification_status": status,
                    "detected_label": detected_label,
                }

        class FakeRecorder:
            enabled = True

        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"notify_prefer_video": prefer_video, "save_dir": tempfile.gettempdir()}
        detector.recorder = FakeRecorder() if prefer_video else None
        detector._last_motion_rect_norm = None
        detector.event_store = FakeStore()
        detector.classifier = FakeClassifier()
        detector._classified_events = set()
        detector._notified_events = set()
        detector._finalized_events = set()
        detector._automation_fired = set()
        detector._live_classify_attempts = {}
        detector._LIVE_CLASSIFY_MAX = 12
        detector._last_classified_notify_at = None
        detector.notifier = object()
        detector.notified = []
        detector._notify_event = lambda event_id, result: detector.notified.append(event_id)
        detector._deliver_event_notification = (
            lambda event_id, result, video_path=None: detector.notified.append(event_id)
        )
        return detector

    def test_ok_status_defers_notification_until_event_close(self):
        detector = self._detector("ok")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])
        detector._finalize_closed_event_notification(
            "ev1",
            "persona",
            {"classification_status": "ok", "detected_label": "persona"},
            should_notify=True,
        )
        self.assertEqual(detector.notified, ["ev1__persona"])

    def test_unavailable_status_notifies_on_close(self):
        detector = self._detector("unavailable")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])
        detector._finalize_closed_event_notification(
            "ev1",
            "movimento",
            {"classification_status": "unavailable"},
            should_notify=True,
        )
        self.assertEqual(detector.notified, ["ev1__movimento"])

    def test_ok_status_defers_photo_when_video_preferred(self):
        # With a clip preferred, the snapshot must NOT fire on classify; the close
        # callback delivers the alert once the event is archived.
        detector = self._detector("ok", prefer_video=True)
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])

    def test_category_disabled_after_classification_suppresses_notify(self):
        # Event classified as person (ok) but person was toggled off afterwards:
        # the current targets no longer include persona, so do not notify.
        detector = self._detector("ok", detected_label="persona", targets={"animale_domestico"})
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])

    def test_should_notify_for_rechecks_current_targets(self):
        detector = self._detector("ok", targets={"animale_domestico"})
        self.assertTrue(detector._should_notify_for({"classification_status": "ok"}))
        self.assertFalse(
            detector._should_notify_for(
                {"classification_status": "ok", "detected_label": "persona"}
            )
        )
        self.assertTrue(
            detector._should_notify_for(
                {"classification_status": "ok", "detected_label": "animale_domestico"}
            )
        )

    def test_ignored_status_does_not_notify(self):
        detector = self._detector("ignored")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])

    def test_no_detection_status_notifies_on_close(self):
        detector = self._detector("no_detection")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])
        detector._finalize_closed_event_notification(
            "ev1",
            "movimento",
            {"classification_status": "no_detection"},
            should_notify=True,
        )
        self.assertEqual(detector.notified, ["ev1__movimento"])

    def test_live_automation_fires_during_event_on_person(self):
        detector = self._detector("ok", detected_label="persona")

        class FakeAutomation:
            def __init__(self):
                self.emitted = []

            def emit(self, ctx):
                self.emitted.append(ctx.category)

        automation = FakeAutomation()
        detector.automation = automation
        detector._automation_fired = set()
        detector._live_classify_attempts = {}

        detector._classify_event_frame("ev1", object())
        detector._classify_event_frame("ev1", object())  # second frame: must not re-fire

        self.assertEqual(automation.emitted, ["persona"])

    def test_emit_automation_skipped_after_live_fire(self):
        detector = self._detector("ok", detected_label="persona")

        class FakeAutomation:
            def __init__(self):
                self.emitted = []

            def emit(self, ctx):
                self.emitted.append(ctx.category)

        automation = FakeAutomation()
        detector.automation = automation
        detector._automation_fired = {"ev1"}
        detector._emit_automation("ev1__persona", "persona", None)
        self.assertEqual(automation.emitted, [])

    def test_tail_motion_suppressed_after_persona_alert(self):
        detector = self._detector("ok")
        detector.config["notify_tail_suppress_sec"] = 30
        detector._note_classified_notification("persona")
        self.assertFalse(
            detector._should_notify_for(
                {"classification_status": "no_detection", "class_label": "unknown"}
            )
        )

    def test_task_close_event_finalizes_once(self):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        detector = self._detector("ok")
        detector.config["save_dir"] = tmpdir
        detector.event_store = self.app_module.MotionEventStore(
            {"save_dir": tmpdir, "save_frames": True, "event_gap": 5.0}
        )
        detector._finalized_events = set()
        detector.recorder = None
        counts = []
        detector._finalize_closed_event_notification = lambda *a, **k: counts.append(1)
        detector._task_close_event("ev1")
        detector._task_close_event("ev1")
        self.assertEqual(len(counts), 1)

    def test_classify_best_from_event_finds_late_person_in_clip(self):
        import cv2
        import numpy as np

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        event_dir = Path(tmpdir) / "motion_event_test"
        event_dir.mkdir()
        clip = event_dir / "event.mp4"
        writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (320, 240))
        for _ in range(10):
            writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
        writer.write(np.full((240, 320, 3), 200, dtype=np.uint8))
        writer.release()

        detector = self._detector("ok")
        detector.config["save_dir"] = tmpdir

        class ClipClassifier:
            enabled = True
            ready = True
            calls = 0

            def classify(self, frame):
                self.calls += 1
                if frame.mean() > 10:
                    return {
                        "class_label": "persona",
                        "detected_label": "persona",
                        "confidence": 0.9,
                        "classification_status": "ok",
                    }
                return {
                    "class_label": "unknown",
                    "classification_status": "no_detection",
                }

        clip_clf = ClipClassifier()
        detector.classifier = clip_clf
        result = detector._classify_best_from_event(event_dir)
        self.assertEqual(result.get("detected_label"), "persona")
        # Con lo stride (1 frame classificato ogni 2) il frame "tardivo" a
        # indice 10 viene comunque raggiunto, con circa meta' delle inferenze.
        self.assertGreaterEqual(clip_clf.calls, 4)
        self.assertLess(clip_clf.calls, 10)


class ClosedEventNotificationTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="closed-notify-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _detector(self, *, prefer_video=True):
        class FakeNotifier:
            def __init__(self):
                self.calls = []

            def notify_event_video(self, **kwargs):
                self.calls.append(("video", kwargs))
                return True

            def notify_event(self, **kwargs):
                self.calls.append(("photo", kwargs))
                return True

        notifier = FakeNotifier()
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {
            "notify_prefer_video": prefer_video,
            "save_dir": self.tmpdir,
        }
        detector.event_store = self.app_module.MotionEventStore(
            {"save_dir": self.tmpdir, "save_frames": True, "event_gap": 5.0}
        )
        detector.notifier = notifier
        detector._notified_events = set()
        detector.recorder = type("R", (), {"enabled": prefer_video})()
        return detector, notifier

    def _seed_event(self, event_id: str, *, with_video=False):
        event_dir = Path(self.tmpdir) / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "cover.jpg").write_bytes(b"jpg")
        (event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).touch()
        if with_video:
            (event_dir / "event.mp4").write_bytes(b"mp4")

    def test_finalize_falls_back_to_photo_when_video_missing(self):
        detector, notifier = self._detector(prefer_video=True)
        self._seed_event("motion_event_20260624_120000")
        detector._finalize_closed_event_notification(
            "motion_event_20260624_120000",
            "persona",
            {"classification_status": "ok", "detected_label": "persona"},
            should_notify=True,
            video_path=None,
        )
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][0], "photo")
        self.assertTrue((Path(self.tmpdir) / "motion_event_20260624_120000__persona").is_dir())

    def test_finalize_sends_video_from_renamed_event_dir(self):
        detector, notifier = self._detector(prefer_video=True)
        event_id = "motion_event_20260624_120000"
        self._seed_event(event_id, with_video=True)
        stale_clip = Path(self.tmpdir) / event_id / "event.mp4"
        detector._finalize_closed_event_notification(
            event_id,
            "persona",
            {"classification_status": "ok", "detected_label": "persona"},
            should_notify=True,
            video_path=stale_clip,
        )
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][0], "video")
        sent_path = notifier.calls[0][1]["video_path"]
        self.assertTrue(
            sent_path.endswith("motion_event_20260624_120000__persona\\event.mp4")
            or sent_path.endswith("motion_event_20260624_120000__persona/event.mp4")
        )
        self.assertTrue(Path(sent_path).is_file())

    def test_mark_notified_resolves_renamed_event_dir(self):
        store = self.app_module.MotionEventStore(
            {"save_dir": self.tmpdir, "save_frames": True, "event_gap": 5.0}
        )
        event_dir = Path(self.tmpdir) / "motion_event_20260624_120000"
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "cover.jpg").write_bytes(b"jpg")
        renamed = store.rename_event_with_label("motion_event_20260624_120000", "persona")
        store.mark_event_notified("motion_event_20260624_120000")
        self.assertTrue(store.event_was_notified(renamed))


class EventLabelTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="motion-label-")
        self.store = self.app_module.MotionEventStore({"save_dir": self.tmpdir})

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _make_event(self, name, meta=None):
        event_dir = Path(self.tmpdir) / name
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "cover.jpg").write_bytes(b"jpg")
        (event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).touch()
        if meta is not None:
            (event_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return event_dir

    def test_rename_appends_category_suffix(self):
        self._make_event("motion_event_20260617_120000")
        new_id = self.store.rename_event_with_label("motion_event_20260617_120000", "persona")
        self.assertEqual(new_id, "motion_event_20260617_120000__persona")
        self.assertTrue((Path(self.tmpdir) / new_id).is_dir())

    def test_rename_is_idempotent_and_rejects_unknown_label(self):
        self._make_event("motion_event_20260617_120000__persona")
        # already labelled -> unchanged
        same = self.store.rename_event_with_label(
            "motion_event_20260617_120000__persona", "persona"
        )
        self.assertEqual(same, "motion_event_20260617_120000__persona")
        # unknown label -> no-op
        self._make_event("motion_event_20260617_130000")
        unchanged = self.store.rename_event_with_label("motion_event_20260617_130000", "gatto")
        self.assertEqual(unchanged, "motion_event_20260617_130000")

    def test_build_event_category_from_meta_detected_label(self):
        # An "ignored" person event still carries detected_label persona for filtering.
        self._make_event(
            "motion_event_20260617_120000",
            meta={"classification": {"class_label": "unknown", "detected_label": "persona"}},
        )
        events = self.store.list_events(limit=5)
        self.assertEqual(events[0]["category"], "persona")

    def test_build_event_category_defaults_to_motion(self):
        self._make_event("motion_event_20260617_120000")
        events = self.store.list_events(limit=5)
        self.assertEqual(events[0]["category"], "movimento")

    def test_build_event_category_falls_back_to_class_label(self):
        # Events classified before detected_label existed still resolve by class_label.
        self._make_event(
            "motion_event_20260617_120000",
            meta={"classification": {"class_label": "animale_domestico"}},
        )
        events = self.store.list_events(limit=5)
        self.assertEqual(events[0]["category"], "animale_domestico")

    def test_resolve_event_label(self):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        self.assertEqual(detector._resolve_event_label({"detected_label": "persona"}), "persona")
        self.assertEqual(
            detector._resolve_event_label({"detected_label": "animale_domestico"}),
            "animale_domestico",
        )
        self.assertEqual(detector._resolve_event_label({"detected_label": None}), "movimento")
        self.assertEqual(detector._resolve_event_label({}), "movimento")


class MotionDetectorEventTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="motion-events-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def build_detector(self, event_gap=3.0):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {
            "save_dir": self.tmpdir,
            "event_gap": event_gap,
            "max_event_duration": 45.0,
        }
        return detector

    def create_file(self, relative_path: str):
        path = Path(self.tmpdir) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        return path

    def test_list_events_returns_recent_first(self):
        self.create_file("motion_event_20260417_120001/cover.jpg")
        self.create_file("motion_event_20260417_120001/latest.jpg")
        self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")
        self.create_file(
            f"motion_event_20260417_120001/{self.app_module.MotionEventStore.CLOSED_MARKER_NAME}"
        )
        self.create_file("motion_event_20260417_120101/cover.jpg")
        self.create_file("motion_event_20260417_120101/latest.jpg")
        self.create_file("motion_event_20260417_120101/frame_20260417_120101_001.jpg")
        self.create_file(
            f"motion_event_20260417_120101/{self.app_module.MotionEventStore.CLOSED_MARKER_NAME}"
        )

        detector = self.build_detector()

        events = detector.list_events(limit=5, include_frames=False)

        self.assertEqual(
            [event["id"] for event in events],
            ["motion_event_20260417_120101", "motion_event_20260417_120001"],
        )

    def test_legacy_frames_group_into_single_event_within_gap(self):
        self.create_file("motion_20260417_120001.jpg")
        self.create_file("motion_20260417_120003.jpg")
        self.create_file("motion_20260417_120010.jpg")

        detector = self.build_detector(event_gap=3.0)

        events = detector.list_events(limit=5, include_frames=True)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["frame_count"], 1)
        self.assertEqual(events[1]["frame_count"], 2)
        self.assertEqual(
            events[1]["frames"],
            [
                "/motion_capture/motion_20260417_120001.jpg",
                "/motion_capture/motion_20260417_120003.jpg",
            ],
        )

    def test_open_event_is_hidden_until_closed(self):
        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        self.create_file("motion_event_20260417_120001/cover.jpg")
        self.create_file("motion_event_20260417_120001/latest.jpg")
        self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")

        detector = self.build_detector(event_gap=30.0)

        self.assertEqual(detector.list_events(limit=5, include_frames=False), [])

        (event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).touch()
        events = detector.list_events(limit=5, include_frames=False)

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["preview_path"].endswith("cover.jpg"))

    def test_ensure_preview_image_extracts_cover_from_clip(self):
        import cv2
        import numpy as np

        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        clip = event_dir / "event.mp4"
        writer = cv2.VideoWriter(
            str(clip),
            cv2.VideoWriter_fourcc(*"mp4v"),
            5.0,
            (64, 48),
        )
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
        writer.release()

        store = self.app_module.MotionEventStore(
            {"save_dir": self.tmpdir, "save_frames": True, "event_gap": 5.0}
        )
        (event_dir / store.CLOSED_MARKER_NAME).touch()
        self.assertTrue(store.ensure_preview_image(event_dir.name))
        events = store.list_events(limit=5)
        self.assertEqual(len(events), 1)
        self.assertTrue((event_dir / "cover.jpg").exists())

    def test_stale_open_event_is_auto_closed_on_read(self):
        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        frame = self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")
        cover = self.create_file("motion_event_20260417_120001/cover.jpg")
        latest = self.create_file("motion_event_20260417_120001/latest.jpg")
        stale_at = time.time() - 10
        for path in (event_dir, frame, cover, latest):
            os.utime(path, (stale_at, stale_at))

        detector = self.build_detector(event_gap=1.0)
        events = detector.list_events(limit=5, include_frames=False)

        self.assertEqual(len(events), 1)
        self.assertTrue((event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).exists())

    def test_stale_current_event_is_auto_closed_on_read(self):
        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        frame = self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")
        cover = self.create_file("motion_event_20260417_120001/cover.jpg")
        latest = self.create_file("motion_event_20260417_120001/latest.jpg")
        stale_at = time.time() - 10
        for path in (event_dir, frame, cover, latest):
            os.utime(path, (stale_at, stale_at))

        detector = self.build_detector(event_gap=1.0)
        detector.event_store = self.app_module.MotionEventStore(detector.config)
        detector.event_store.current_event_dir = event_dir
        detector.event_store.current_event_id = event_dir.name
        detector.event_store.current_event_last_at = stale_at
        detector.event_store.current_event_started_at = stale_at

        events = detector.list_events(limit=5, include_frames=False)

        self.assertEqual(len(events), 1)
        self.assertIsNone(detector.event_store.current_event_dir)
        self.assertTrue((event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).exists())

    def test_max_event_duration_rolls_over_to_new_event(self):
        store = self.app_module.MotionEventStore(
            {
                "save_frames": True,
                "save_dir": self.tmpdir,
                "event_gap": 30.0,
                "max_event_duration": 1.0,
            }
        )

        with mock.patch.object(self.app_module.cv2, "imwrite", return_value=True):
            with mock.patch.object(self.app_module.time, "time", side_effect=[0.0, 2.0]):
                _, first_event_id = store.save_frame(object(), "20260417_120001")
                _, second_event_id = store.save_frame(object(), "20260417_120010")

        self.assertNotEqual(first_event_id, second_event_id)
        self.assertTrue((Path(self.tmpdir) / first_event_id / store.CLOSED_MARKER_NAME).exists())

    def test_open_event_creates_directory_without_frame(self):
        store = self.app_module.MotionEventStore(
            {
                "save_frames": True,
                "save_dir": self.tmpdir,
                "event_gap": 30.0,
                "max_event_duration": 45.0,
            }
        )
        event_id, event_dir = store.open_event("20260417_120001")
        self.assertEqual(event_id, "motion_event_20260417_120001")
        self.assertTrue(event_dir.is_dir())
        self.assertEqual(list(event_dir.glob("frame_*.jpg")), [])

    def test_clear_all_handles_current_event_without_crashing(self):
        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        self.create_file("motion_event_20260417_120001/cover.jpg")
        self.create_file("motion_event_20260417_120001/latest.jpg")
        self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")

        store = self.app_module.MotionEventStore(
            {
                "save_frames": True,
                "save_dir": self.tmpdir,
                "event_gap": 30.0,
                "max_event_duration": 45.0,
            }
        )
        store.current_event_id = event_dir.name
        store.current_event_dir = event_dir
        store.current_event_last_at = time.time()
        store.current_event_started_at = time.time()

        removed = store.clear_all()

        self.assertEqual(removed, 1)
        self.assertFalse(event_dir.exists())
        self.assertIsNone(store.current_event_dir)

    def test_active_event_capture_continues_while_scene_settles(self):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"clear_frames": 12, "capture_interval": 0.75, "min_interval": 0.30}
        detector.motion_detected = True
        detector.clear_streak = 4
        detector.last_capture_saved_at = 10.0

        self.assertFalse(detector._should_save_active_event_frame(10.5))
        self.assertTrue(detector._should_save_active_event_frame(10.8))

    def test_active_event_capture_stops_after_quiet_window(self):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"clear_frames": 12, "capture_interval": 0.75, "min_interval": 0.30}
        detector.motion_detected = True
        detector.clear_streak = 12
        detector.last_capture_saved_at = 10.0

        self.assertFalse(detector._should_save_active_event_frame(11.2))

    def test_global_lighting_change_is_ignored(self):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"min_area": 3200, "max_area_ratio": 0.45}

        self.assertFalse(detector._is_usable_motion_area(500000, 1000000))
        self.assertTrue(detector._is_usable_motion_area(120000, 1000000))

    def _make_detect_detector(self, **overrides):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {
            "scale_width": 0,
            "blur_size": 5,
            "mog2_history": 50,
            "mog2_var_threshold": 35,
            "morph_kernel": 3,
            "morph_dilate_iter": 2,
            "global_change_ratio": 0.5,
            "min_area": 600,
            "max_area_ratio": 0.95,
            "learning_rate": -1,
            "learning_rate_active": 0.0005,
        }
        detector.config.update(overrides)
        detector.motion_detected = False
        detector._build_subtractor()
        return detector

    def _warmup(self, detector, base, frames=40):
        import numpy as np

        rng = np.random.default_rng(0)
        for _ in range(frames):
            noisy = np.clip(
                base.astype("int16") + rng.normal(0, 4, base.shape).astype("int16"), 0, 255
            ).astype("uint8")
            detector._detect_motion(noisy)

    def test_detect_motion_ignores_gaussian_noise(self):
        import numpy as np

        base = np.full((240, 320, 3), 120, dtype="uint8")
        detector = self._make_detect_detector()
        self._warmup(detector, base)

        rng = np.random.default_rng(99)
        noisy = np.clip(
            base.astype("int16") + rng.normal(0, 4, base.shape).astype("int16"), 0, 255
        ).astype("uint8")
        motion_now, area, _ = detector._detect_motion(noisy)

        self.assertFalse(motion_now)
        self.assertEqual(area, 0.0)

    def test_detect_motion_triggers_on_moved_blob(self):
        import numpy as np

        base = np.full((240, 320, 3), 120, dtype="uint8")
        detector = self._make_detect_detector()
        self._warmup(detector, base)

        triggered = False
        last_area = 0.0
        for _ in range(5):
            frame = base.copy()
            import cv2

            cv2.rectangle(frame, (130, 90), (200, 160), (255, 255, 255), -1)
            motion_now, last_area, _ = detector._detect_motion(frame)
            triggered = triggered or motion_now

        self.assertTrue(triggered)
        self.assertGreater(last_area, detector.config["min_area"])

    def test_detect_motion_ignores_global_lighting_shift(self):
        import numpy as np

        base = np.full((240, 320, 3), 120, dtype="uint8")
        detector = self._make_detect_detector()
        self._warmup(detector, base)

        shifted = np.clip(base.astype("int16") + 60, 0, 255).astype("uint8")
        motion_now, area, _ = detector._detect_motion(shifted)

        self.assertFalse(motion_now)
        self.assertEqual(area, 0.0)

    def test_saved_event_exposes_classification_metadata(self):
        event_dir = Path(self.tmpdir) / "motion_event_20260417_120001"
        event_dir.mkdir(parents=True, exist_ok=True)
        self.create_file("motion_event_20260417_120001/cover.jpg")
        self.create_file("motion_event_20260417_120001/latest.jpg")
        self.create_file("motion_event_20260417_120001/frame_20260417_120001_001.jpg")
        self.create_file(
            f"motion_event_20260417_120001/{self.app_module.MotionEventStore.CLOSED_MARKER_NAME}"
        )
        (event_dir / "meta.json").write_text(
            json.dumps(
                {
                    "classification": {
                        "class_label": "persona",
                        "confidence": 0.93,
                        "backend": "local",
                    }
                }
            ),
            encoding="utf-8",
        )

        detector = self.build_detector()
        events = detector.list_events(limit=5, include_frames=False)

        self.assertEqual(events[0]["classification"]["class_label"], "persona")
        self.assertEqual(events[0]["classification"]["backend"], "local")

    def test_event_classification_is_written_once_per_event(self):
        class FakeStore:
            def __init__(self):
                self.meta_writes = 0
                self.current_event_dir = None

            def save_frame(self, frame, timestamp):
                return "/tmp/frame.jpg", "motion_event_20260417_120001"

            def save_event_meta(self, event_id, data):
                self.meta_writes += 1

            def event_has_classification(self, event_id):
                return self.meta_writes > 0

        class FakeClassifier:
            enabled = True
            sample_policy = "event_cover"

            def classify(self, frame):
                return {
                    "class_label": "persona",
                    "confidence": 0.88,
                    "classification_status": "ok",
                }

        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {}
        detector._last_motion_rect_norm = None
        detector.event_store = FakeStore()
        detector.classifier = FakeClassifier()
        detector._classified_events = set()
        detector._notified_events = set()
        detector._automation_fired = set()
        detector._live_classify_attempts = {}
        detector._LIVE_CLASSIFY_MAX = 12
        detector.recorder = None
        detector.notifier = None
        detector.last_event_id = None
        detector.last_capture_saved_at = None

        detector._save_motion_frame(object(), "20260417_120001")
        detector._save_motion_frame(object(), "20260417_120002")

        self.assertEqual(detector.event_store.meta_writes, 1)

    def test_classification_does_not_run_when_motion_disabled(self):
        class FakeStore:
            current_event_dir = None

            def save_frame(self, frame, timestamp):
                return "/tmp/frame.jpg", "motion_event_20260417_120001"

            def save_event_meta(self, event_id, data):
                raise AssertionError("classification must not run when motion is disabled")

            def event_has_classification(self, event_id):
                return False

        class FakeClassifier:
            enabled = True
            sample_policy = "event_cover"

            def classify(self, frame):
                raise AssertionError("classifier must not be invoked when motion is disabled")

        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"enabled": False}
        detector._last_motion_rect_norm = None
        detector.event_store = FakeStore()
        detector.classifier = FakeClassifier()
        detector._classified_events = set()
        detector._notified_events = set()
        detector.recorder = None
        detector.notifier = None
        detector.last_event_id = None
        detector.last_capture_saved_at = None

        # Must not raise: classification_on is gated by motion being enabled.
        detector._save_motion_frame(object(), "20260417_120001")


class MultiCameraRegistryTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_profile_motion_dir_is_isolated_and_sanitized(self):
        self.assertEqual(
            self.app_module.profile_motion_dir("cam-2"),
            str(Path("captures/motion") / "cam-2"),
        )
        # Unsafe characters are stripped.
        self.assertEqual(
            self.app_module.profile_motion_dir("a/b c"),
            str(Path("captures/motion") / "abc"),
        )

    def test_rtsp_url_and_onvif_fallback_from_profile(self):
        profile = {
            "host": "10.0.0.5",
            "rtsp_port": 554,
            "stream_path": "stream2",
            "username": "u",
            "password": "p",
            "onvif_port": 2020,
            "onvif_username": "",
            "onvif_password": "",
        }
        self.assertEqual(
            self.app_module.rtsp_url_from_profile(profile),
            "rtsp://u:p@10.0.0.5:554/stream2",
        )
        onvif = self.app_module.onvif_config_from_profile(profile)
        # Empty ONVIF creds fall back to the RTSP credentials.
        self.assertEqual(onvif["username"], "u")
        self.assertEqual(onvif["password"], "p")

    def test_camera_and_motion_resolves_active_and_monitors(self):
        from types import SimpleNamespace

        class FakeProfiles:
            def get_active_profile_id(self):
                return "A"

        monitor_camera = object()
        monitor_motion = object()
        runtime = self.app_module.CameraRuntime(
            profile_id="B",
            camera=monitor_camera,
            motion=monitor_motion,
            recorder=None,
            ptz=None,
        )
        services = self.app_module.AppServices(
            camera="active-cam",
            ptz=None,
            motion="active-motion",
            features=SimpleNamespace(camera_profiles=FakeProfiles()),
            runtime_config=None,
            monitors={"B": runtime},
        )

        self.assertEqual(services.camera_and_motion("A"), ("active-cam", "active-motion"))
        self.assertEqual(services.camera_and_motion("B"), (monitor_camera, monitor_motion))
        self.assertEqual(services.camera_and_motion("missing"), (None, None))

    def _build_two_profile_app(self):
        tmpdir = tempfile.mkdtemp(prefix="camera-view-monitor-")
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService(str(Path(tmpdir) / "presets.json")),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService(str(Path(tmpdir) / "rec")),
            camera_profiles=self.app_module.CameraProfileService(
                str(Path(tmpdir) / "profiles.json")
            ),
            wifi=self.app_module.WifiService(),
        )
        base = {
            "host": "192.168.1.40",
            "rtsp_port": 554,
            "stream_path": "stream1",
            "username": "user",
            "password": "pass",
            "onvif_port": 2020,
            "move_speed": 0.6,
            "move_timeout": 0.35,
        }
        active = features.camera_profiles.save_profile({**base, "name": "Attiva", "activate": True})
        secondary = features.camera_profiles.save_profile({**base, "name": "Secondaria"})
        services = self.app_module.AppServices(
            camera=object(),
            ptz=object(),
            motion=object(),
            features=features,
            runtime_config=object(),
            monitors={secondary["id"]: object()},
        )
        app = self.app_module.create_app(services)
        return app, active, secondary

    def test_camera_view_renders_monitored_secondary_without_redirect(self):
        app, _active, secondary = self._build_two_profile_app()
        client = app.test_client()
        authenticate_client(client)

        response = client.get(f"/camera/{secondary['id']}")

        self.assertEqual(response.status_code, 200)
        # Live-only view: feed base points at the per-camera endpoints.
        self.assertIn(f"/cam/{secondary['id']}".encode(), response.data)

    def test_camera_view_redirects_unmonitored_secondary_to_active(self):
        app, active, _secondary = self._build_two_profile_app()
        # Drop the monitor so the secondary is neither active nor monitored.
        app.config["services"].monitors = {}
        # Create a third, unmonitored profile.
        features = app.config["services"].features
        third = features.camera_profiles.save_profile(
            {
                "name": "Terza",
                "host": "192.168.1.41",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "pass",
                "onvif_port": 2020,
                "move_speed": 0.6,
                "move_timeout": 0.35,
            }
        )
        client = app.test_client()
        authenticate_client(client)

        response = client.get(f"/camera/{third['id']}")

        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/camera/{active['id']}", response.headers["Location"])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_even_blur_size_becomes_odd(self):
        with mock.patch.dict(os.environ, {"MOTION_BLUR_SIZE": "30"}, clear=False):
            config = self.app_module.get_motion_config()

        self.assertEqual(config["blur_size"], 31)

    def test_default_min_area_is_tuned_for_people_and_pets(self):
        with mock.patch.dict(os.environ, {"MOTION_MIN_AREA": ""}, clear=False):
            os.environ.pop("MOTION_MIN_AREA", None)
            config = self.app_module.get_motion_config()

        # Min area is now relative to the downscaled detection frame (MOTION_SCALE_WIDTH).
        self.assertEqual(config["min_area"], 600)

    def test_default_capture_interval_keeps_active_event_multiframe(self):
        with mock.patch.dict(os.environ, {"MOTION_CAPTURE_INTERVAL": ""}, clear=False):
            os.environ.pop("MOTION_CAPTURE_INTERVAL", None)
            config = self.app_module.get_motion_config()

        self.assertEqual(config["capture_interval"], 0.18)

    def test_default_max_area_ratio_filters_global_scene_flicker(self):
        with mock.patch.dict(os.environ, {"MOTION_MAX_AREA_RATIO": ""}, clear=False):
            os.environ.pop("MOTION_MAX_AREA_RATIO", None)
            config = self.app_module.get_motion_config()

        self.assertEqual(config["max_area_ratio"], 0.45)

    def test_default_event_max_duration_caps_runaway_event_length(self):
        with mock.patch.dict(os.environ, {"MOTION_EVENT_MAX_DURATION": ""}, clear=False):
            os.environ.pop("MOTION_EVENT_MAX_DURATION", None)
            config = self.app_module.get_motion_config()

        self.assertEqual(config["max_event_duration"], 45.0)


class CameraProfileServiceTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="camera-profile-store-")
        self.store_path = Path(self.tmpdir) / "profiles.json"
        self.key_path = Path(self.tmpdir) / ".profiles.key"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _build_service(self):
        return self.app_module.CameraProfileService(
            str(self.store_path),
            key_path=self.key_path,
        )

    def test_save_profile_rejects_host_with_injection_chars(self):
        service = self._build_service()
        for bad_host in ["evil.com/@1.2.3.4", "10.0.0.1 -rtsp_transport", "a@b", "1.2.3.4:9999"]:
            with self.assertRaises(ValueError):
                service.save_profile(
                    {"name": "X", "host": bad_host, "username": "u", "password": "p"}
                )

    def test_save_profile_rejects_stream_path_with_control_chars(self):
        service = self._build_service()
        with self.assertRaises(ValueError):
            service.save_profile(
                {
                    "name": "X",
                    "host": "192.168.1.5",
                    "stream_path": "stream1 evil",
                    "username": "u",
                    "password": "p",
                }
            )

    def test_profile_passwords_are_encrypted_at_rest(self):
        service = self._build_service()

        saved = service.save_profile(
            {
                "name": "Camera Studio",
                "wifi_ssid": "Studio",
                "host": "192.168.1.77",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "super-secret-pass",
                "onvif_port": 2020,
                "onvif_username": "user",
                "onvif_password": "super-secret-onvif",
                "move_speed": 0.6,
                "move_timeout": 0.35,
            }
        )
        raw_text = self.store_path.read_text(encoding="utf-8")
        loaded = service.get_profile(saved["id"])

        self.assertNotIn("super-secret-pass", raw_text)
        self.assertNotIn("super-secret-onvif", raw_text)
        self.assertIn("enc::", raw_text)
        self.assertEqual(loaded["password"], "super-secret-pass")
        self.assertEqual(loaded["onvif_password"], "super-secret-onvif")
        self.assertEqual(self.store_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.key_path.stat().st_mode & 0o777, 0o600)

    def test_edit_keeps_existing_password_when_blank(self):
        service = self._build_service()
        saved = service.save_profile(
            {
                "name": "Cam",
                "host": "192.168.1.10",
                "username": "u",
                "password": "secret-rtsp",
                "onvif_password": "secret-onvif",
            }
        )
        # Edit changing only the host, leaving secrets blank.
        service.save_profile(
            {
                "id": saved["id"],
                "name": "Cam rinominata",
                "host": "192.168.1.99",
                "username": "u",
                "password": "",
                "onvif_password": "",
            }
        )
        updated = service.get_profile(saved["id"])
        self.assertEqual(updated["host"], "192.168.1.99")
        self.assertEqual(updated["name"], "Cam rinominata")
        self.assertEqual(updated["password"], "secret-rtsp")
        self.assertEqual(updated["onvif_password"], "secret-onvif")

    def test_delete_profile_reassigns_active(self):
        service = self._build_service()
        a = service.save_profile(
            {"name": "A", "host": "10.0.0.1", "username": "u", "password": "p"}
        )
        b = service.save_profile(
            {"name": "B", "host": "10.0.0.2", "username": "u", "password": "p"}
        )
        service.activate_profile(a["id"])

        new_active = service.delete_profile(a["id"])

        self.assertEqual(new_active, b["id"])
        self.assertIsNone(service.get_profile(a["id"]))
        with self.assertRaises(ValueError):
            service.delete_profile("does-not-exist")

    def test_plaintext_profile_store_is_migrated_on_read(self):
        plaintext = {
            "active_profile_id": "cam-1",
            "profiles": [
                {
                    "id": "cam-1",
                    "name": "Legacy",
                    "wifi_ssid": "",
                    "host": "192.168.1.55",
                    "rtsp_port": 554,
                    "stream_path": "stream1",
                    "username": "legacy-user",
                    "password": "legacy-pass",
                    "onvif_port": 2020,
                    "onvif_username": "legacy-user",
                    "onvif_password": "legacy-onvif",
                    "move_speed": 0.6,
                    "move_timeout": 0.35,
                    "notes": "",
                }
            ],
        }
        self.store_path.write_text(json.dumps(plaintext, indent=2), encoding="utf-8")

        service = self._build_service()
        profile = service.get_profile("cam-1")
        migrated_text = self.store_path.read_text(encoding="utf-8")

        self.assertEqual(profile["password"], "legacy-pass")
        self.assertEqual(profile["onvif_password"], "legacy-onvif")
        self.assertNotIn("legacy-pass", migrated_text)
        self.assertNotIn("legacy-onvif", migrated_text)
        self.assertIn("enc::", migrated_text)

    def test_profile_store_can_migrate_from_keyfile_to_explicit_env_key(self):
        service = self._build_service()
        saved = service.save_profile(
            {
                "name": "Camera Studio",
                "wifi_ssid": "Studio",
                "host": "192.168.1.77",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "old-secret-pass",
                "onvif_port": 2020,
                "onvif_username": "user",
                "onvif_password": "old-secret-onvif",
                "move_speed": 0.6,
                "move_timeout": 0.35,
            }
        )
        old_store = self.store_path.read_text(encoding="utf-8")
        new_env_key = Fernet.generate_key().decode("utf-8")

        with mock.patch.dict(
            os.environ,
            {"APP_PROFILE_ENCRYPTION_KEY": new_env_key},
            clear=False,
        ):
            migrated_service = self.app_module.CameraProfileService(
                str(self.store_path),
                key_path=self.key_path,
            )
            loaded = migrated_service.get_profile(saved["id"])

        new_store = self.store_path.read_text(encoding="utf-8")
        self.assertEqual(loaded["password"], "old-secret-pass")
        self.assertEqual(loaded["onvif_password"], "old-secret-onvif")
        self.assertNotEqual(old_store, new_store)


class AppFactoryTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_create_app_uses_injected_services(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return [{"id": "evt", "preview_path": __file__}]

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        authenticate_client(client)

        motion_response = client.get("/motion_captures?limit=1")
        stream_response = client.get("/stream_status")

        self.assertEqual(motion_response.status_code, 200)
        self.assertEqual(motion_response.get_json()["total"], 1)
        self.assertEqual(stream_response.status_code, 200)
        self.assertTrue(stream_response.get_json()["connected"])

    def test_build_services_applies_active_profile_runtime_updates_before_startup(self):
        class FakeCameraProfiles:
            def ensure_default_profile(self, profile):
                return profile

            def get_active_profile_id(self):
                return "env-default"

            def get_profile(self, profile_id):
                return {"id": profile_id}

            def build_runtime_updates(self, profile):
                return {"MOTION_SAVE_DIR": f"captures/motion/{profile['id']}"}

        runtime_update_calls = []

        with mock.patch.object(
            self.app_module,
            "CameraProfileService",
            return_value=FakeCameraProfiles(),
        ):
            with mock.patch.object(self.app_module, "RuntimeConfigManager") as runtime_cls:
                runtime_instance = runtime_cls.return_value
                runtime_instance.update.side_effect = (
                    lambda updates,
                    allow_sensitive=False,
                    allow_internal=False: runtime_update_calls.append(
                        (updates, allow_sensitive, allow_internal)
                    )
                )
                with mock.patch.object(
                    self.app_module, "get_rtsp_url", return_value="rtsp://example"
                ):
                    with mock.patch.object(self.app_module, "get_stream_config", return_value={}):
                        with mock.patch.object(
                            self.app_module, "get_onvif_config", return_value={}
                        ):
                            with mock.patch.object(
                                self.app_module,
                                "get_motion_config",
                                return_value={"save_dir": "captures/motion"},
                            ):
                                with mock.patch.object(self.app_module, "CameraStream"):
                                    with mock.patch.object(self.app_module, "PTZController"):
                                        with mock.patch.object(self.app_module, "MotionDetector"):
                                            self.app_module.build_services()

        self.assertEqual(
            runtime_update_calls,
            [({"MOTION_SAVE_DIR": "captures/motion/env-default"}, True, True)],
        )

    def test_index_route_renders(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        authenticate_client(client)
        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        page = response.data.decode("utf-8")
        # Viewer slim: restano i quick toggle e il link alla pagina
        # Impostazioni; la configurazione completa vive in /impostazioni.
        self.assertIn('id="cfg-record-enabled"', page)
        self.assertIn('id="cfg-notify-telegram-enabled"', page)
        self.assertIn('href="/impostazioni"', page)
        self.assertNotIn('id="cfg-classification-enabled"', page)
        self.assertNotIn('id="telegram-dialog"', page)

    def test_runtime_config_endpoints_update_and_apply(self):
        class FakeCamera:
            def __init__(self):
                self.last_updates = None

            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakePtz:
            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def __init__(self):
                self.last_updates = None
                self.cleared = False

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

            def clear_events(self):
                self.cleared = True
                return 7

        class FakeRuntimeConfig:
            def __init__(self):
                self.current = {
                    "MOTION_ENABLED": True,
                    "MOTION_THRESHOLD": 35,
                    "MOTION_MIN_AREA": 1400,
                    "CLASSIFICATION_ENABLED": False,
                    "CLASSIFICATION_BACKEND": "local",
                    "CLASSIFICATION_MIN_CONFIDENCE": 0.55,
                    "CLASSIFICATION_SAMPLE_POLICY": "event_cover",
                }

            def get_public_config(self):
                return self.current

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                self.current.update(updates)
                return self.current

            def public_update_keys(self):
                return RuntimeConfigManager().public_update_keys()

        fake_camera = FakeCamera()
        fake_ptz = FakePtz()
        fake_motion = FakeMotion()
        fake_runtime = FakeRuntimeConfig()

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=fake_camera,
                ptz=fake_ptz,
                motion=fake_motion,
                features=build_feature_services(self.app_module),
                runtime_config=fake_runtime,
            )
        )
        client = app.test_client()
        csrf_token = authenticate_client(client)

        get_response = client.get("/runtime_config")
        patch_response = client.patch(
            "/api/runtime_config",
            json={
                "updates": {
                    "MOTION_ENABLED": False,
                    "MOTION_THRESHOLD": 55,
                    "MOTION_MIN_AREA": 2100,
                    "CLASSIFICATION_ENABLED": True,
                    "CLASSIFICATION_BACKEND": "cloud",
                    "CLASSIFICATION_MIN_CONFIDENCE": 0.8,
                    "CLASSIFICATION_SAMPLE_POLICY": "event_cover",
                }
            },
            headers=csrf_headers(csrf_token),
        )
        delete_response = client.delete(
            "/api/motion_captures",
            headers=csrf_headers(csrf_token),
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json()["config"]["MOTION_ENABLED"], True)
        self.assertEqual(get_response.get_json()["config"]["MOTION_THRESHOLD"], 35)
        self.assertEqual(get_response.get_json()["config"]["MOTION_MIN_AREA"], 1400)
        self.assertEqual(get_response.get_json()["config"]["CLASSIFICATION_ENABLED"], False)
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.get_json()["config"]["MOTION_ENABLED"], False)
        self.assertEqual(patch_response.get_json()["config"]["MOTION_THRESHOLD"], 55)
        self.assertEqual(patch_response.get_json()["config"]["MOTION_MIN_AREA"], 2100)
        self.assertEqual(patch_response.get_json()["config"]["CLASSIFICATION_ENABLED"], True)
        self.assertEqual(patch_response.get_json()["config"]["CLASSIFICATION_BACKEND"], "cloud")
        self.assertEqual(patch_response.get_json()["config"]["CLASSIFICATION_MIN_CONFIDENCE"], 0.8)
        self.assertEqual(fake_camera.last_updates["MOTION_ENABLED"], False)
        self.assertEqual(fake_ptz.last_updates["MOTION_THRESHOLD"], 55)
        self.assertEqual(fake_ptz.last_updates["MOTION_MIN_AREA"], 2100)
        self.assertEqual(fake_ptz.last_updates["CLASSIFICATION_BACKEND"], "cloud")
        self.assertEqual(fake_motion.last_updates["MOTION_THRESHOLD"], 55)
        self.assertEqual(fake_motion.last_updates["MOTION_MIN_AREA"], 2100)
        self.assertEqual(fake_motion.last_updates["CLASSIFICATION_ENABLED"], True)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.get_json()["removed"], 7)
        self.assertTrue(fake_motion.cleared)

    def test_runtime_config_endpoint_rejects_internal_path_override(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                return None

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                return None

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

            def clear_events(self):
                return 0

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": True, **updates}

            def public_update_keys(self):
                return RuntimeConfigManager().public_update_keys()

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        csrf_token = authenticate_client(client)

        response = client.patch(
            "/api/runtime_config",
            json={"updates": {"MOTION_SAVE_DIR": "/"}},
            headers=csrf_headers(csrf_token),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Parametro non consentito", response.get_json()["error"])

    def test_camera_profiles_endpoint_saves_and_activates_profile(self):
        class FakeCamera:
            def __init__(self):
                self.last_updates = None

            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakePtz:
            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def __init__(self):
                self.last_updates = None

            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                self.last_updates = updates
                return {"MOTION_ENABLED": True}

        class FakeWifiService:
            def get_current_wifi(self):
                return {
                    "connected": True,
                    "ssid": "Casa Papa",
                    "interface": "en0",
                    "source": "test",
                    "error": "",
                }

        tmpdir = tempfile.mkdtemp(prefix="camera-profiles-")
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))

        fake_camera = FakeCamera()
        fake_ptz = FakePtz()
        fake_runtime = FakeRuntimeConfig()
        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService("data/test-presets.json"),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService("captures/test-recordings"),
            camera_profiles=self.app_module.CameraProfileService(
                str(Path(tmpdir) / "profiles.json")
            ),
            wifi=FakeWifiService(),
        )

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=fake_camera,
                ptz=fake_ptz,
                motion=FakeMotion(),
                features=features,
                runtime_config=fake_runtime,
            )
        )
        client = app.test_client()
        csrf_token = authenticate_client(client)

        response = client.post(
            "/api/cameras",
            json={
                "name": "Tapo Papa",
                "wifi_ssid": "Casa Papa",
                "host": "192.168.1.88",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "pass",
                "onvif_port": 2020,
                "onvif_username": "",
                "onvif_password": "",
                "move_speed": 0.7,
                "move_timeout": 0.4,
                "activate": True,
            },
            headers=csrf_headers(csrf_token),
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["current_wifi"]["ssid"], "Casa Papa")
        self.assertEqual(len(payload["profiles"]), 1)
        self.assertEqual(payload["profiles"][0]["host"], "192.168.1.88")
        self.assertNotIn("password", payload["profiles"][0])
        self.assertEqual(fake_runtime.last_updates["TAPO_HOST"], "192.168.1.88")
        self.assertIn("captures/motion", fake_runtime.last_updates["MOTION_SAVE_DIR"])
        self.assertEqual(fake_runtime.last_updates["TAPO_ONVIF_PASSWORD"], "pass")
        self.assertEqual(fake_camera.last_updates["TAPO_PASSWORD"], "pass")
        self.assertEqual(fake_ptz.last_updates["TAPO_ONVIF_USERNAME"], "user")

    def test_camera_manager_and_viewer_routes_render(self):
        class FakeCamera:
            def __init__(self):
                self.last_updates = None

            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakePtz:
            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeRuntimeConfig:
            def __init__(self):
                self.last_updates = None

            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                self.last_updates = updates
                return {"MOTION_ENABLED": True}

        tmpdir = tempfile.mkdtemp(prefix="camera-view-routes-")
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))

        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService("data/test-presets.json"),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService("captures/test-recordings"),
            camera_profiles=self.app_module.CameraProfileService(
                str(Path(tmpdir) / "profiles.json")
            ),
            wifi=self.app_module.WifiService(),
        )
        saved = features.camera_profiles.save_profile(
            {
                "name": "Tapo Studio",
                "wifi_ssid": "Studio",
                "host": "192.168.1.40",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "pass",
                "onvif_port": 2020,
                "move_speed": 0.6,
                "move_timeout": 0.35,
                "activate": True,
            }
        )

        fake_camera = FakeCamera()
        fake_ptz = FakePtz()
        fake_motion = FakeMotion()
        fake_runtime = FakeRuntimeConfig()

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=fake_camera,
                ptz=fake_ptz,
                motion=fake_motion,
                features=features,
                runtime_config=fake_runtime,
            )
        )
        client = app.test_client()
        csrf_token = authenticate_client(client)

        manager_response = client.get("/cameras")
        activate_response = client.post(
            f"/api/cameras/{saved['id']}/activate",
            headers=csrf_headers(csrf_token),
        )
        viewer_response = client.get(saved["viewer_url"])
        # Pagina orfana rimossa nel consolidamento UI: la guida sul modello
        # vive nella sezione Riconoscimento della pagina Impostazioni.
        training_response = client.get("/model-training")

        self.assertEqual(manager_response.status_code, 200)
        self.assertEqual(activate_response.status_code, 200)
        self.assertEqual(viewer_response.status_code, 200)
        self.assertEqual(training_response.status_code, 404)
        self.assertEqual(fake_camera.last_updates["TAPO_HOST"], "192.168.1.40")
        self.assertIn("captures/motion", fake_motion.last_updates["MOTION_SAVE_DIR"])

    def test_viewer_route_does_not_mutate_runtime_state(self):
        class FakeCamera:
            def __init__(self):
                self.last_updates = None

            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakePtz:
            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def __init__(self):
                self.last_updates = None

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                self.last_updates = updates

        class FakeRuntimeConfig:
            def __init__(self):
                self.last_updates = None

            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                self.last_updates = updates
                return {"MOTION_ENABLED": True}

        tmpdir = tempfile.mkdtemp(prefix="camera-view-no-mutate-")
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))

        features = self.app_module.FeatureServices(
            presets=self.app_module.PresetService("data/test-presets.json"),
            notifications=self.app_module.NotificationService(),
            recording=self.app_module.RecordingService("captures/test-recordings"),
            camera_profiles=self.app_module.CameraProfileService(
                str(Path(tmpdir) / "profiles.json")
            ),
            wifi=self.app_module.WifiService(),
        )
        saved = features.camera_profiles.save_profile(
            {
                "name": "Tapo Studio",
                "wifi_ssid": "Studio",
                "host": "192.168.1.40",
                "rtsp_port": 554,
                "stream_path": "stream1",
                "username": "user",
                "password": "pass",
                "onvif_port": 2020,
                "move_speed": 0.6,
                "move_timeout": 0.35,
                "activate": True,
            }
        )

        fake_camera = FakeCamera()
        fake_ptz = FakePtz()
        fake_motion = FakeMotion()
        fake_runtime = FakeRuntimeConfig()

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=fake_camera,
                ptz=fake_ptz,
                motion=fake_motion,
                features=features,
                runtime_config=fake_runtime,
            )
        )
        client = app.test_client()
        authenticate_client(client)

        response = client.get(saved["viewer_url"])

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(fake_runtime.last_updates)
        self.assertIsNone(fake_camera.last_updates)
        self.assertIsNone(fake_ptz.last_updates)
        self.assertIsNone(fake_motion.last_updates)

    def test_protected_api_requires_authentication(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": True}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()

        response = client.get("/stream_status")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["ok"], False)


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        import blackframe.auth as auth

        auth.rate_limiter._events.clear()

    def _build_app(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

            def apply_runtime_config(self, updates):
                return None

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

            def apply_runtime_config(self, updates):
                return None

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

            def clear_events(self):
                return 0

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": True}

        return self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )

    def test_login_requires_explicit_admin_password(self):
        with mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "",
                "APP_SECRET_KEY": "test-secret",
                "TAPO_PASSWORD": "camera-pass",
            },
            clear=False,
        ):
            app = self._build_app()
            client = app.test_client()
            response = client.post("/login", data={"password": "camera-pass", "next": "/"})

        self.assertEqual(response.status_code, 503)
        self.assertIn(b"APP_ADMIN_PASSWORD", response.data)

    def test_login_rate_limit_blocks_repeated_failures(self):
        with mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "admin-pass",
                "APP_SECRET_KEY": "test-secret",
            },
            clear=False,
        ):
            app = self._build_app()
            client = app.test_client()
            for _ in range(5):
                response = client.post("/login", data={"password": "wrong", "next": "/"})
                self.assertEqual(response.status_code, 401)

            blocked = client.post("/login", data={"password": "wrong", "next": "/"})

        self.assertEqual(blocked.status_code, 429)
        self.assertGreaterEqual(int(blocked.headers.get("Retry-After", "0")), 1)

    def test_login_rate_limit_not_bypassed_by_forwarded_for(self):
        # Without APP_TRUST_PROXY the client cannot rotate X-Forwarded-For to dodge
        # the per-IP throttle: all requests collapse to the same remote_addr.
        with mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "admin-pass",
                "APP_SECRET_KEY": "test-secret",
            },
            clear=False,
        ):
            os.environ.pop("APP_TRUST_PROXY", None)
            import blackframe.auth as auth

            auth.rate_limiter._events.clear()
            app = self._build_app()
            client = app.test_client()
            for index in range(5):
                response = client.post(
                    "/login",
                    data={"password": "wrong", "next": "/"},
                    headers={"X-Forwarded-For": f"10.0.0.{index}"},
                )
                self.assertEqual(response.status_code, 401)

            blocked = client.post(
                "/login",
                data={"password": "wrong", "next": "/"},
                headers={"X-Forwarded-For": "10.0.0.99"},
            )

        self.assertEqual(blocked.status_code, 429)

    def test_security_headers_include_csp(self):
        app = self._build_app()
        client = app.test_client()

        response = client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_rate_limiter_evicts_keys_above_cap(self):
        # A flood of distinct keys must not grow the dict without bound.
        import blackframe.auth as auth

        limiter = auth.RateLimiter()
        with mock.patch.object(auth.RateLimiter, "_MAX_KEYS", 5):
            for index in range(50):
                allowed, _ = limiter.allow(f"login:ip{index}", limit=10, window_seconds=300)
                self.assertTrue(allowed)
            self.assertLessEqual(len(limiter._events), 5)


class StreamApiTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _build_app_with_camera(self, camera):
        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

            def move(self, direction):
                return True, ""

            def stop(self):
                return True, ""

            def home(self):
                return True, ""

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

            def get_event(self, event_id):
                return {"id": event_id}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        return self.app_module.create_app(
            self.app_module.AppServices(
                camera=camera,
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )

    def test_stream_status_payload_is_backward_compatible_and_extended(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {
                    "connected": False,
                    "frame_age_seconds": None,
                    "error": "Connessione RTSP in corso...",
                    "connection_state": "connecting",
                    "reconnect_count": 3,
                }

            def get_diagnostics(self):
                return {"ok": True}

        app = self._build_app_with_camera(FakeCamera())
        client = app.test_client()
        authenticate_client(client)
        response = client.get("/stream_status")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("connected", payload)
        self.assertIn("frame_age_seconds", payload)
        self.assertIn("error", payload)
        self.assertEqual(payload["connection_state"], "connecting")
        self.assertEqual(payload["reconnect_count"], 3)

    def test_stream_diagnostics_endpoint_returns_payload(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "frame_age_seconds": 0.1, "error": ""}

            def get_diagnostics(self):
                return {
                    "connected": True,
                    "connection_state": "online",
                    "stream_config": {
                        "open_timeout_sec": 8.0,
                        "reconnect_backoff_max_sec": 15.0,
                    },
                }

        app = self._build_app_with_camera(FakeCamera())
        client = app.test_client()
        authenticate_client(client)
        response = client.get("/stream_diagnostics")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["connection_state"], "online")
        self.assertEqual(payload["stream_config"]["open_timeout_sec"], 8.0)


class CameraStreamStateTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _build_camera_for_status_checks(self):
        camera = self.app_module.CameraStream.__new__(self.app_module.CameraStream)
        camera.rtsp_url = "rtsp://user:pass@192.168.1.120:554/stream1"
        camera.open_timeout_sec = 8.0
        camera.reconnect_backoff_max_sec = 15.0
        camera.snapshot_interval_online_ms = 700
        camera.snapshot_interval_offline_ms = 2500
        camera.capture = None
        camera.frame = None
        camera.raw_frame = None
        camera.last_frame_at = None
        camera.last_success_at = None
        camera.last_error = "In attesa del primo frame..."
        camera.last_error_stage = "open"
        camera.connection_state = "offline"
        camera.open_attempts = 1
        camera.open_failures = 1
        camera.read_failures = 0
        camera.reconnect_count = 1
        camera.last_connect_attempt_at = None
        camera.last_open_error_at = None
        camera.last_read_error_at = None
        camera.next_retry_in_seconds = 1.0
        camera._retry_delay_seconds = 2.0
        camera.lock = threading.Lock()
        camera._raw_ready = threading.Condition(camera.lock)
        camera._stopped = threading.Event()
        camera.raw_sequence = 0
        return camera

    def test_status_transitions_offline_connecting_online(self):
        camera = self._build_camera_for_status_checks()

        offline = camera.get_status()
        self.assertFalse(offline["connected"])
        self.assertEqual(offline["connection_state"], "offline")
        self.assertEqual(offline["snapshot_interval_ms"], 2500)

        with camera.lock:
            camera.connection_state = "connecting"
            camera.last_error = "Connessione RTSP in corso..."
        connecting = camera.get_status()
        self.assertFalse(connecting["connected"])
        self.assertEqual(connecting["connection_state"], "connecting")
        self.assertEqual(connecting["snapshot_interval_ms"], 700)

        with camera.lock:
            camera.connection_state = "online"
            camera.frame = b"jpeg-bytes"
            camera.raw_frame = b"raw-frame"
            camera.last_frame_at = time.time()
            camera.last_success_at = camera.last_frame_at
            camera.last_error = ""
            camera.last_error_stage = ""
        online = camera.get_status()
        self.assertTrue(online["connected"])
        self.assertEqual(online["connection_state"], "online")
        self.assertEqual(online["snapshot_interval_ms"], 700)
        self.assertIsNone(online["error"] or None)

    def test_raw_frame_packet_uses_raw_sequence(self):
        # Il recorder cattura dai raw frame: get_raw_frame_packet deve restituire
        # raw_sequence (avanza ad ogni frame), non frame_sequence (solo all'encode).
        camera = self._build_camera_for_status_checks()
        camera.raw_frame = [1, 2, 3]  # oggetto con .copy(); basta per il test
        camera.raw_sequence = 42
        camera.frame_sequence = 7
        _frame, seq = camera.get_raw_frame_packet()
        self.assertEqual(seq, 42)

    def test_wait_for_raw_frame_wakes_without_polling(self):
        camera = self._build_camera_for_status_checks()

        def publish():
            time.sleep(0.02)
            with camera._raw_ready:
                camera.raw_frame = [4, 5, 6]
                camera.raw_sequence = 1
                camera._raw_ready.notify_all()

        thread = threading.Thread(target=publish)
        thread.start()
        frame, sequence = camera.wait_for_raw_frame(0, 0.5)
        thread.join()

        self.assertEqual(frame, [4, 5, 6])
        self.assertEqual(sequence, 1)

    def test_get_stream_config_encode_interval_matches_record_fps(self):
        # encode_interval = min(snapshot, 1000/record_fps). Con fps 10 e snapshot 700 -> 100ms.
        env = {"STREAM_SNAPSHOT_INTERVAL_ONLINE_MS": "700", "RECORD_FPS": "10"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = self.app_module.get_stream_config()
        self.assertEqual(cfg["encode_interval_ms"], 100)

    def test_get_stream_config_encode_interval_override(self):
        env = {"STREAM_ENCODE_INTERVAL_MS": "250", "RECORD_FPS": "10"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = self.app_module.get_stream_config()
        self.assertEqual(cfg["encode_interval_ms"], 250)


class MotionEventWorkerTests(unittest.TestCase):
    """Il worker eventi esegue i task pesanti fuori dal lock di detection."""

    def setUp(self):
        self.app_module = load_app_module()

    def _bare_detector(self, *, start_worker: bool):
        det = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        det._stopped = threading.Event()
        det._event_queue = deque()
        det._event_queue_lock = threading.Lock()
        det._event_queue_ready = threading.Condition(det._event_queue_lock)
        if start_worker:
            t = threading.Thread(target=det._event_worker_loop, daemon=True)
            t.start()
            self.addCleanup(det.stop)
        return det

    def _wait(self, condition, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_worker_runs_fifo_and_isolates_errors(self):
        det = self._bare_detector(start_worker=True)
        results = []

        def _boom():
            raise RuntimeError("task fallito")

        det._enqueue_event(_boom, critical=True)  # non deve uccidere il worker
        det._enqueue_event(lambda: results.append("a"))
        det._enqueue_event(lambda: results.append("b"))
        self.assertTrue(self._wait(lambda: results == ["a", "b"]), f"results={results}")

    def test_backpressure_drops_noncritical_keeps_critical(self):
        det = self._bare_detector(start_worker=False)  # nessun drain: coda resta piena
        for _ in range(det._EVENT_QUEUE_MAX):
            det._enqueue_event(lambda: None, critical=False)
        self.assertEqual(len(det._event_queue), det._EVENT_QUEUE_MAX)
        det._enqueue_event(lambda: None, critical=False)  # scartato
        self.assertEqual(len(det._event_queue), det._EVENT_QUEUE_MAX)
        det._enqueue_event(lambda: None, critical=True)
        self.assertLessEqual(len(det._event_queue), det._EVENT_QUEUE_TOTAL_MAX)

    def test_total_queue_is_bounded_even_for_critical_tasks(self):
        det = self._bare_detector(start_worker=False)
        for _ in range(det._EVENT_QUEUE_TOTAL_MAX + 5):
            det._enqueue_event(lambda: None, critical=True)
        self.assertEqual(len(det._event_queue), det._EVENT_QUEUE_TOTAL_MAX)


class RuntimeConfigManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="runtime-config-")
        self.env_path = Path(self.tmpdir) / ".env"
        self.env_path.write_text(
            "\n".join(
                [
                    "TAPO_USERNAME=user",
                    "TAPO_PASSWORD=pass",
                    "TAPO_HOST=192.168.1.10",
                    "MOTION_ENABLED=true",
                    "MOTION_MIN_AREA=1400",
                    "MOTION_THRESHOLD=35",
                    "MOTION_BLUR_SIZE=30",
                    "CLASSIFICATION_ENABLED=false",
                    "CLASSIFICATION_BACKEND=local",
                    "CLASSIFICATION_MIN_CONFIDENCE=0.55",
                    "CLASSIFICATION_SAMPLE_POLICY=event_cover",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_update_persists_env_and_coerces_values(self):
        manager = RuntimeConfigManager(self.env_path)
        config = manager.update(
            {
                "MOTION_ENABLED": False,
                "MOTION_MIN_AREA": "2100",
                "MOTION_THRESHOLD": "51",
                "MOTION_BLUR_SIZE": "30",
            }
        )
        env_text = self.env_path.read_text(encoding="utf-8")

        self.assertEqual(config["MOTION_ENABLED"], False)
        self.assertEqual(config["MOTION_MIN_AREA"], 2100)
        self.assertEqual(config["MOTION_THRESHOLD"], 51)
        self.assertEqual(config["MOTION_BLUR_SIZE"], 31)
        self.assertIn("MOTION_ENABLED=false", env_text)
        self.assertIn("MOTION_MIN_AREA=2100", env_text)
        self.assertIn("MOTION_THRESHOLD=51", env_text)
        self.assertIn("MOTION_BLUR_SIZE=31", env_text)

    def test_update_rejects_unknown_key(self):
        manager = RuntimeConfigManager(self.env_path)
        with self.assertRaises(ValueError):
            manager.update({"TAPO_PASSWORD": "new-pass"})

    def test_update_rejects_internal_only_key_without_explicit_override(self):
        manager = RuntimeConfigManager(self.env_path)
        with self.assertRaises(ValueError):
            manager.update({"MOTION_SAVE_DIR": "/tmp"})

    def test_update_rejects_control_characters_in_strings(self):
        manager = RuntimeConfigManager(self.env_path)
        with self.assertRaises(ValueError):
            manager.update({"TAPO_HOST": "127.0.0.1\nEVIL=1"})

    def test_update_accepts_classification_settings(self):
        manager = RuntimeConfigManager(self.env_path)
        config = manager.update(
            {
                "CLASSIFICATION_ENABLED": True,
                "CLASSIFICATION_BACKEND": "local",
                "CLASSIFICATION_MIN_CONFIDENCE": "0.7",
                "CLASSIFICATION_SAMPLE_POLICY": "event_cover",
            }
        )
        env_text = self.env_path.read_text(encoding="utf-8")

        self.assertEqual(config["CLASSIFICATION_ENABLED"], True)
        self.assertEqual(config["CLASSIFICATION_BACKEND"], "local")
        self.assertEqual(config["CLASSIFICATION_MIN_CONFIDENCE"], 0.7)
        self.assertEqual(config["CLASSIFICATION_SAMPLE_POLICY"], "event_cover")
        self.assertIn("CLASSIFICATION_ENABLED=true", env_text)
        self.assertIn("CLASSIFICATION_BACKEND=local", env_text)

    def test_update_rejects_invalid_classification_backend(self):
        manager = RuntimeConfigManager(self.env_path)
        with self.assertRaises(ValueError):
            manager.update({"CLASSIFICATION_BACKEND": "unsupported"})


class TelegramConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _build_client(self, recorded_updates, telegram_status=None):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def apply_runtime_config(self, updates):
                return None

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                recorded_updates.append((dict(updates), allow_sensitive))
                return {}

        class FakeTelegram:
            def status(self):
                return telegram_status or {
                    "enabled": False,
                    "configured": False,
                    "classes": [],
                    "min_interval_sec": 30,
                }

        features = build_feature_services(self.app_module)
        features.telegram = FakeTelegram()
        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=features,
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        csrf = authenticate_client(client)
        return client, csrf

    def test_status_never_returns_token(self):
        client, _ = self._build_client([])
        with mock.patch.dict(os.environ, {"NOTIFY_TELEGRAM_BOT_TOKEN": "secret"}, clear=False):
            response = client.get("/api/telegram_config")
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["has_token"])
        self.assertNotIn("bot_token", data)
        self.assertNotIn("secret", json.dumps(data))

    def test_enable_without_token_is_rejected(self):
        recorded = []
        client, csrf = self._build_client(recorded)
        with mock.patch.dict(os.environ, {"NOTIFY_TELEGRAM_BOT_TOKEN": ""}, clear=False):
            response = client.post(
                "/api/telegram_config",
                json={"enabled": True, "chat_id": "123"},
                headers=csrf_headers(csrf),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(recorded, [])

    def test_save_persists_sensitive_fields(self):
        recorded = []
        client, csrf = self._build_client(recorded)
        response = client.post(
            "/api/telegram_config",
            json={
                "bot_token": "111:AAA",
                "chat_id": "123456",
                "enabled": True,
                "prefer_video": False,
            },
            headers=csrf_headers(csrf),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(recorded), 1)
        updates, allow_sensitive = recorded[0]
        self.assertTrue(allow_sensitive)
        self.assertEqual(updates["NOTIFY_TELEGRAM_BOT_TOKEN"], "111:AAA")
        self.assertEqual(updates["NOTIFY_TELEGRAM_CHAT_ID"], "123456")
        self.assertIs(updates["NOTIFY_TELEGRAM_ENABLED"], True)
        self.assertIs(updates["NOTIFY_PREFER_VIDEO"], False)

    def test_save_rejects_malformed_bot_token(self):
        recorded = []
        client, csrf = self._build_client(recorded)
        response = client.post(
            "/api/telegram_config",
            json={"bot_token": "111:AAA/../evil", "chat_id": "123"},
            headers=csrf_headers(csrf),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(recorded, [])

    def test_discover_uses_body_token(self):
        client, csrf = self._build_client([])
        with mock.patch(
            "blackframe.routes.motion.discover_telegram_chats",
            return_value=(True, [{"chat_id": 7, "label": "Me (private)"}], None),
        ) as discover:
            response = client.post(
                "/api/telegram_discover",
                json={"bot_token": "999:ZZZ"},
                headers=csrf_headers(csrf),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["chats"][0]["chat_id"], 7)
        discover.assert_called_once_with("999:ZZZ")

    def test_test_endpoint_requires_token_and_chat(self):
        client, csrf = self._build_client([])
        with mock.patch.dict(
            os.environ,
            {"NOTIFY_TELEGRAM_BOT_TOKEN": "", "NOTIFY_TELEGRAM_CHAT_ID": ""},
            clear=False,
        ):
            response = client.post(
                "/api/telegram_test",
                json={},
                headers=csrf_headers(csrf),
            )
        self.assertEqual(response.status_code, 400)


class OnvifAndFootageHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_onvif_xml_parser_settings_are_hardened(self):
        # The camera's SOAP responses are untrusted XML; the parser must forbid
        # external refs/DTDs/entities and ignore onvif-zeep enabling xml_huge_tree.
        self.app_module._harden_onvif_xml_parser()
        import onvif.client as onvif_client

        settings = onvif_client.Settings()
        settings.xml_huge_tree = True  # onvif-zeep forces this after construction
        self.assertTrue(settings.forbid_external)
        self.assertTrue(settings.forbid_dtd)
        self.assertTrue(settings.forbid_entities)
        self.assertFalse(settings.xml_huge_tree)

    def test_onvif_transport_blocks_remote_document_loads(self):
        transport = self.app_module._build_onvif_transport()
        for url in ("http://169.254.169.254/latest/meta-data/", "https://evil.example/x.xsd"):
            with self.assertRaises(RuntimeError):
                transport.load(url)

    def test_harden_captures_permissions_makes_footage_private(self):
        tmp = Path(tempfile.mkdtemp(prefix="footage-"))
        try:
            (tmp / "motion_event_20260101_000000").mkdir()
            clip = tmp / "motion_event_20260101_000000" / "frame.jpg"
            clip.write_bytes(b"x")
            os.chmod(tmp, 0o755)
            os.chmod(clip, 0o644)
            with mock.patch.dict(os.environ, {"MOTION_SAVE_DIR": str(tmp)}, clear=False):
                self.app_module.harden_captures_permissions()
            self.assertEqual(os.stat(tmp).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(clip).st_mode & 0o777, 0o600)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NetworkAndCredentialHardeningTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_private_runtime_file_permissions_are_hardened(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests = Path(tmp) / "guests.json"
            guests.write_text("{}")
            os.chmod(guests, 0o644)
            with mock.patch.dict(
                os.environ,
                {"TELEGRAM_GUESTS_FILE": str(guests)},
                clear=False,
            ):
                self.app_module.harden_private_runtime_files()
            self.assertEqual(os.stat(guests).st_mode & 0o777, 0o600)

    def test_opencv_thread_cap_is_applied(self):
        with (
            mock.patch.dict(os.environ, {"OPENCV_NUM_THREADS": "1"}, clear=False),
            mock.patch.object(self.app_module.cv2, "setNumThreads") as set_threads,
            mock.patch.object(self.app_module.cv2, "setUseOptimized") as optimized,
        ):
            self.app_module.configure_opencv_runtime()
        set_threads.assert_called_once_with(1)
        optimized.assert_called_once_with(True)

    def test_rtsp_url_percent_encodes_credentials(self):
        url = self.app_module.rtsp_url_from_profile(
            {
                "username": "a@b",
                "password": "p@:/ss",
                "host": "10.0.0.1",
                "rtsp_port": 554,
                "stream_path": "stream1",
            }
        )
        self.assertEqual(url, "rtsp://a%40b:p%40%3A%2Fss@10.0.0.1:554/stream1")
        self.assertNotIn("p@:/ss", url)

    def test_cloud_endpoint_metadata_guard(self):
        import blackframe.classification as classification

        # IP literals: no DNS needed, works offline.
        self.assertTrue(classification._endpoint_targets_metadata("http://169.254.169.254/latest/"))
        self.assertFalse(classification._endpoint_targets_metadata("http://93.184.216.34/predict"))

    def test_session_has_absolute_lifetime(self):
        from datetime import timedelta

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=mock.MagicMock(),
                ptz=mock.MagicMock(),
                motion=mock.MagicMock(),
                features=build_feature_services(self.app_module),
                runtime_config=mock.MagicMock(),
            )
        )
        self.assertIsInstance(app.config["PERMANENT_SESSION_LIFETIME"], timedelta)
        self.assertGreater(app.config["PERMANENT_SESSION_LIFETIME"].total_seconds(), 0)

    def test_forwarded_for_garbage_does_not_bypass_rate_limit(self):
        import blackframe.auth as auth

        with mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "admin-pass",
                "APP_SECRET_KEY": "test-secret",
                "APP_TRUST_PROXY": "true",
            },
            clear=False,
        ):
            auth.rate_limiter._events.clear()
            app = self.app_module.create_app(
                self.app_module.AppServices(
                    camera=mock.MagicMock(),
                    ptz=mock.MagicMock(),
                    motion=mock.MagicMock(),
                    features=build_feature_services(self.app_module),
                    runtime_config=mock.MagicMock(),
                )
            )
            client = app.test_client()
            for _ in range(5):
                r = client.post(
                    "/login",
                    data={"password": "wrong", "next": "/"},
                    headers={"X-Forwarded-For": "not-an-ip"},
                )
                self.assertEqual(r.status_code, 401)
            blocked = client.post(
                "/login",
                data={"password": "wrong", "next": "/"},
                headers={"X-Forwarded-For": "also/garbage"},
            )
        self.assertEqual(blocked.status_code, 429)


class NavConsistencyTests(unittest.TestCase):
    """Ogni pagina autenticata usa la nav condivisa (_nav.html): voce corrente
    marcata una sola volta, link a tutte le altre pagine sempre presenti."""

    def setUp(self):
        self.app_module = load_app_module()

    def _client(self):
        class FakeCamera:
            def get_frame(self):
                return b"frame"

            def get_status(self):
                return {"connected": True, "error": ""}

        class FakePtz:
            def get_status(self):
                return {"available": True, "host": "127.0.0.1", "port": 2020, "error": ""}

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

            def list_events(self, limit=8, include_frames=False):
                return []

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {"MOTION_ENABLED": True}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=build_feature_services(self.app_module),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        authenticate_client(client)
        return client

    def test_every_page_marks_current_once_and_links_the_rest(self):
        client = self._client()
        pages = {
            "/": "Viewer",
            "/dashboard": "Dashboard",
            "/cameras": "Camere",
            "/automazione": "Automazione",
            "/agente": "Agente",
            "/impostazioni": "Impostazioni",
        }
        for url, label in pages.items():
            response = client.get(url, follow_redirects=True)
            self.assertEqual(response.status_code, 200, url)
            html = response.data.decode("utf-8")
            self.assertEqual(html.count('aria-current="page"'), 1, url)
            for other_url, other_label in pages.items():
                if other_url == url:
                    continue
                self.assertIn(f'href="{other_url}"', html, f"{url} -> {other_url}")
            self.assertIn(">Esci<", html, url)

    def test_login_page_has_no_nav(self):
        client = self._client()
        with client.session_transaction() as session_state:
            session_state.clear()
        response = client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("app-header-nav", response.data.decode("utf-8"))


class DashboardTilesTests(unittest.TestCase):
    """I tile della dashboard usano snapshot aggiornati dal poller, non N
    stream MJPEG simultanei (ognuno pinna un thread del server)."""

    def setUp(self):
        self.app_module = load_app_module()

    def test_tiles_use_snapshot_not_mjpeg(self):
        tmpdir = tempfile.mkdtemp(prefix="dash-profiles-")
        self.addCleanup(shutil.rmtree, tmpdir)
        features = build_feature_services(self.app_module)
        # Store profili isolato: ensure_default_profile sul file condiviso
        # renderebbe attivo un profilo per i test successivi.
        features = self.app_module.FeatureServices(
            presets=features.presets,
            notifications=features.notifications,
            recording=features.recording,
            camera_profiles=self.app_module.CameraProfileService(
                str(Path(tmpdir) / "profiles.json")
            ),
            wifi=features.wifi,
        )
        features.camera_profiles.ensure_default_profile(
            self.app_module.build_default_camera_profile()
        )

        class FakeMotion:
            config = {"save_dir": tempfile.gettempdir()}

            def get_status(self):
                return {"enabled": True}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=mock.Mock(),
                ptz=mock.Mock(),
                motion=FakeMotion(),
                features=features,
                runtime_config=mock.Mock(),
            )
        )
        client = app.test_client()
        authenticate_client(client)
        response = client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn('data-role="tile-snapshot"', html)
        self.assertIn("/snapshot.jpg", html)
        self.assertNotIn("/video_feed", html)


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_page_requires_auth(self):
        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=mock.Mock(),
                ptz=mock.Mock(),
                motion=mock.Mock(config={"save_dir": tempfile.gettempdir()}),
                features=build_feature_services(self.app_module),
                runtime_config=mock.Mock(),
            )
        )
        client = app.test_client()
        response = client.get("/impostazioni")
        self.assertIn(response.status_code, (302, 401))

    def test_page_renders_all_sections(self):
        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=mock.Mock(),
                ptz=mock.Mock(),
                motion=mock.Mock(config={"save_dir": tempfile.gettempdir()}),
                features=build_feature_services(self.app_module),
                runtime_config=mock.Mock(),
            )
        )
        client = app.test_client()
        authenticate_client(client)
        response = client.get("/impostazioni")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        for section_id in (
            'id="prestazioni"',
            'id="movimento"',
            'id="riconoscimento"',
            'id="registrazione"',
            'id="telegram"',
            'id="agente"',
            'id="sistema"',
        ):
            self.assertIn(section_id, html)


class PublicUpdateKeysTests(unittest.TestCase):
    def test_allowlist_from_single_source(self):
        manager = RuntimeConfigManager()
        keys = manager.public_update_keys()
        # Chiavi storiche della sidebar viewer.
        for key in ("MOTION_ENABLED", "CLASSIFICATION_ENABLED", "RECORD_ENABLED"):
            self.assertIn(key, keys)
        # Nuove chiavi esposte dalla pagina Impostazioni.
        for key in (
            "MOTION_BLUR_SIZE",
            "RECORD_FPS",
            "RECORD_PREROLL_SEC",
            "RECORD_MAX_DURATION_SEC",
            "NOTIFY_MIN_INTERVAL_SEC",
        ):
            self.assertIn(key, keys)
        # Mai sensibili o interne, anche se qualcuno le marcasse ui_editable.
        for key in keys:
            field = manager.fields[key]
            self.assertFalse(field.sensitive, key)
            self.assertFalse(field.internal_only, key)
        self.assertNotIn("TAPO_PASSWORD", keys)
        self.assertNotIn("MOTION_SAVE_DIR", keys)

    def test_patch_accepts_new_key_and_rejects_sensitive(self):
        app_module = load_app_module()

        class FakeRuntimeConfig:
            def get_public_config(self):
                return {}

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                return dict(updates)

            def public_update_keys(self):
                return RuntimeConfigManager().public_update_keys()

        services = app_module.AppServices(
            camera=mock.Mock(),
            ptz=mock.Mock(),
            motion=mock.Mock(config={"save_dir": tempfile.gettempdir()}),
            features=build_feature_services(app_module),
            runtime_config=FakeRuntimeConfig(),
        )
        app = app_module.create_app(services)
        client = app.test_client()
        csrf_token = authenticate_client(client)

        ok_response = client.patch(
            "/api/runtime_config",
            json={"updates": {"RECORD_FPS": 8}},
            headers=csrf_headers(csrf_token),
        )
        self.assertEqual(ok_response.status_code, 200)

        bad_response = client.patch(
            "/api/runtime_config",
            json={"updates": {"TAPO_PASSWORD": "x"}},
            headers=csrf_headers(csrf_token),
        )
        self.assertEqual(bad_response.status_code, 400)


class AutomationSourceContextTests(unittest.TestCase):
    """EventContext deve portare source (camera_id) e timestamp: le regole
    con filtro `source:` altrimenti non scattano mai."""

    def setUp(self):
        self.app_module = load_app_module()

    def _bare_detector(self, camera_id="cam-1"):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.camera_id = camera_id
        detector._automation_fired = set()
        detector.automation = mock.Mock()
        return detector

    def test_live_automation_context_carries_source_and_timestamp(self):
        detector = self._bare_detector()
        detector._fire_live_automation("motion_event_20260417_120001", "persona")
        ctx = detector.automation.emit.call_args[0][0]
        self.assertEqual(ctx.source, "cam-1")
        self.assertIsNotNone(ctx.timestamp)
        self.assertEqual(ctx.category, "persona")

    def test_close_automation_context_carries_source_and_timestamp(self):
        detector = self._bare_detector(camera_id="cam-2")
        detector._emit_automation("motion_event_20260417_120001", "movimento", None)
        ctx = detector.automation.emit.call_args[0][0]
        self.assertEqual(ctx.source, "cam-2")
        self.assertIsNotNone(ctx.timestamp)


class MonitorRuntimeWiringTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_build_camera_runtime_passes_automation_and_camera_id(self):
        engine = mock.Mock()
        profile = {"id": "garage"}
        with (
            mock.patch.object(self.app_module, "CameraStream") as camera_cls,
            mock.patch.object(self.app_module, "EventRecorder"),
            mock.patch.object(self.app_module, "ContinuousRecorder"),
            mock.patch.object(self.app_module, "MotionDetector") as motion_cls,
            mock.patch.object(self.app_module, "rtsp_url_from_profile", return_value="rtsp://x"),
            mock.patch.object(self.app_module, "get_stream_config", return_value={}),
            mock.patch.object(self.app_module, "motion_config_for_profile", return_value={}),
        ):
            runtime = self.app_module.build_camera_runtime(profile, None, engine)
        self.assertIs(motion_cls.call_args.kwargs["automation"], engine)
        self.assertEqual(motion_cls.call_args.kwargs["camera_id"], "garage")
        self.assertEqual(runtime.profile_id, "garage")
        camera_cls.assert_called_once()


class RetentionJanitorSweepTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_sweep_uses_global_root_quota(self):
        good = mock.Mock()
        good.config = {
            "retention_interval_sec": 120,
            "retention_days": 14,
            "retention_max_mb": 5000,
            "save_dir": "captures/motion/attiva",
        }
        good.camera_id = "attiva"
        good._get_event_store.return_value.current_event_dir = None
        broken = mock.Mock()
        broken.config = {
            "retention_interval_sec": 180,
            "retention_days": 14,
            "retention_max_mb": 5000,
            "save_dir": "captures/motion/monitor",
        }
        broken.camera_id = "monitor"
        broken._get_event_store.return_value.current_event_dir = None

        janitor = self.app_module.RetentionJanitor.__new__(self.app_module.RetentionJanitor)
        janitor._provider = lambda: [good, broken]

        with mock.patch.object(janitor, "_purge_root", return_value=2) as purge:
            interval = janitor._sweep()

        purge.assert_called_once()
        self.assertEqual(interval, 120.0)

    def test_global_retention_removes_orphan_profile_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "motion"
            active = root / "active" / "motion_event_active"
            orphan = root / "deleted-profile" / "motion_event_old"
            active.mkdir(parents=True)
            orphan.mkdir(parents=True)
            (active / "frame.jpg").write_bytes(b"a" * 10)
            (orphan / "frame.jpg").write_bytes(b"b" * 10)

            removed = self.app_module.RetentionJanitor._purge_root(
                root, days=0, max_mb=0.000015, active={active.resolve()}
            )

            self.assertEqual(removed, 1)
            self.assertTrue(active.exists())
            self.assertFalse(orphan.exists())


class ContinuousConfigPropagationTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def _services(self):
        active_continuous = mock.Mock()
        monitor_continuous = mock.Mock()
        monitor_continuous.config = {
            "continuous_record_enabled": False,
            "save_dir": "captures/motion/garage",
        }
        monitor = mock.Mock()
        monitor.continuous = monitor_continuous
        services = self.app_module.AppServices(
            camera=mock.Mock(),
            ptz=mock.Mock(),
            motion=mock.Mock(),
            features=mock.Mock(),
            runtime_config=mock.Mock(),
            monitors={"garage": monitor},
            continuous=active_continuous,
        )
        return services, active_continuous, monitor_continuous

    def test_continuous_updates_reach_active_and_monitors(self):
        services, active, monitor = self._services()
        with mock.patch.object(
            self.app_module, "get_motion_config", return_value={"continuous_record_enabled": True}
        ):
            services.apply_runtime_config_all({"CONTINUOUS_RECORD_ENABLED": True})
        active.apply_config.assert_called_once_with({"continuous_record_enabled": True})
        merged = monitor.apply_config.call_args[0][0]
        self.assertTrue(merged["continuous_record_enabled"])
        # La config del monitor deriva dal profilo: save_dir non deve cambiare.
        self.assertEqual(merged["save_dir"], "captures/motion/garage")

    def test_non_continuous_updates_do_not_touch_recorders(self):
        services, active, monitor = self._services()
        services.apply_runtime_config_all({"MOTION_ENABLED": True})
        active.apply_config.assert_not_called()
        monitor.apply_config.assert_not_called()

    def test_string_bool_coerced_for_monitors(self):
        services, _, monitor = self._services()
        with mock.patch.object(self.app_module, "get_motion_config", return_value={}):
            services.apply_runtime_config_all({"CONTINUOUS_RECORD_ENABLED": "true"})
        merged = monitor.apply_config.call_args[0][0]
        self.assertIs(merged["continuous_record_enabled"], True)


class CameraProfileCacheTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="profile-cache-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _service(self):
        return self.app_module.CameraProfileService(
            store_path=str(Path(self.tmpdir) / "profiles.json"),
            key_path=Path(self.tmpdir) / ".profiles.key",
        )

    def test_repeated_reads_hit_cache(self):
        service = self._service()
        service.ensure_default_profile(self.app_module.build_default_camera_profile())
        with mock.patch.object(
            service, "_read_store_uncached", wraps=service._read_store_uncached
        ) as uncached:
            service.get_active_profile_id()
            service.get_active_profile_id()
            service.list_profiles()
        self.assertEqual(uncached.call_count, 1)

    def test_write_invalidates_cache(self):
        service = self._service()
        profile = self.app_module.build_default_camera_profile()
        service.ensure_default_profile(profile)
        service.get_active_profile_id()
        created = service.save_profile(
            {
                "name": "Garage",
                "host": "10.0.0.9",
                "username": "u",
                "password": "p",
            }
        )
        ids = [p["id"] for p in service.list_profiles()]
        self.assertIn(created["id"], ids)

    def test_external_file_change_is_picked_up(self):
        service = self._service()
        service.ensure_default_profile(self.app_module.build_default_camera_profile())
        first = service.get_active_profile_id()
        # Simula modifica esterna: riscrive lo store con altro active id.
        other = self._service()
        data = other._read_store()
        data["active_profile_id"] = "qualcos-altro"
        other._write_store(data)
        self.assertNotEqual(service.get_active_profile_id(), first)

    def test_cached_dict_mutation_does_not_leak(self):
        service = self._service()
        service.ensure_default_profile(self.app_module.build_default_camera_profile())
        data = service._read_store()
        data["active_profile_id"] = "mutato-localmente"
        self.assertNotEqual(service.get_active_profile_id(), "mutato-localmente")


class EventSummaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="summary-cache-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _store(self):
        return self.app_module.MotionEventStore(
            {"save_dir": self.tmpdir, "event_gap": 3.0, "max_event_duration": 45.0}
        )

    def _make_event(self, name):
        event_dir = Path(self.tmpdir) / name
        event_dir.mkdir(parents=True)
        (event_dir / "cover.jpg").write_bytes(b"jpg")
        (event_dir / "latest.jpg").write_bytes(b"jpg")
        (event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).write_bytes(b"")
        return event_dir

    def test_second_listing_does_not_rebuild_summaries(self):
        self._make_event("motion_event_20260417_120001")
        self._make_event("motion_event_20260417_120101")
        store = self._store()
        store.list_events(limit=10)
        with mock.patch.object(
            store, "_build_saved_event", side_effect=AssertionError("cache mancata")
        ):
            events = store.list_events(limit=10)
        self.assertEqual(len(events), 2)

    def test_meta_write_refreshes_summary(self):
        self._make_event("motion_event_20260417_120001")
        store = self._store()
        store.list_events(limit=10)
        store.save_event_meta(
            "motion_event_20260417_120001",
            {"classification": {"detected_label": "persona"}},
        )
        events = store.list_events(limit=10)
        self.assertEqual(events[0]["category"], "persona")

    def test_include_frames_bypasses_summary_cache(self):
        event_dir = self._make_event("motion_event_20260417_120001")
        (event_dir / "frame_20260417_120001_001.jpg").write_bytes(b"jpg")
        store = self._store()
        store.list_events(limit=10)
        events = store.list_events(limit=10, include_frames=True)
        self.assertIn("frames", events[0])


class HardenPermissionsMarkerTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="perms-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_second_run_skips_tree_walk(self):
        cwd = os.getcwd()
        os.chdir(self.tmpdir)
        self.addCleanup(os.chdir, cwd)
        captures = Path(self.tmpdir) / "captures"
        (captures / "motion").mkdir(parents=True)
        (captures / "motion" / "clip.jpg").write_bytes(b"jpg")
        env = {"MOTION_SAVE_DIR": "", "CONTINUOUS_RECORD_DIR": ""}
        with mock.patch.dict(os.environ, env):
            self.app_module.harden_captures_permissions()
            marker = captures / self.app_module._PERMS_MARKER_NAME
            self.assertTrue(marker.exists())
            with mock.patch.object(
                self.app_module.os, "chmod", side_effect=AssertionError("rieseguito")
            ):
                self.app_module.harden_captures_permissions()


class ClipClassificationEarlyExitTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_early_exit_stops_after_confident_subject(self):
        import cv2
        import numpy as np

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir)
        event_dir = Path(tmpdir) / "motion_event_test"
        event_dir.mkdir()
        clip = event_dir / "event.mp4"
        writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (320, 240))
        for _ in range(20):
            writer.write(np.full((240, 320, 3), 200, dtype=np.uint8))
        writer.release()

        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"save_dir": tmpdir}

        class AlwaysPerson:
            enabled = True
            ready = True
            calls = 0

            def classify(self, frame):
                self.calls += 1
                return {
                    "detected_label": "persona",
                    "class_label": "persona",
                    "confidence": 0.95,
                    "classification_status": "ok",
                }

        clf = AlwaysPerson()
        detector.classifier = clf
        result = detector._classify_best_from_event(event_dir)
        self.assertEqual(result["detected_label"], "persona")
        # 0.95 >= soglia 0.85: si ferma alla prima inferenza confidente.
        self.assertEqual(clf.calls, 1)


class GetEventDirectResolutionTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="get-event-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _store(self):
        return self.app_module.MotionEventStore(
            {"save_dir": self.tmpdir, "event_gap": 3.0, "max_event_duration": 45.0}
        )

    def _make_event(self, name, closed=True):
        event_dir = Path(self.tmpdir) / name
        event_dir.mkdir(parents=True)
        (event_dir / "cover.jpg").write_bytes(b"jpg")
        (event_dir / "latest.jpg").write_bytes(b"jpg")
        (event_dir / f"frame_{name.replace('motion_event_', '')}_001.jpg").write_bytes(b"jpg")
        if closed:
            (event_dir / self.app_module.MotionEventStore.CLOSED_MARKER_NAME).write_bytes(b"")
        return event_dir

    def test_get_event_resolves_without_full_listing(self):
        self._make_event("motion_event_20260417_120001")
        self._make_event("motion_event_20260417_120101")
        self._make_event("motion_event_20260417_120201__persona")
        store = self._store()

        with mock.patch.object(
            store, "_iter_saved_events", side_effect=AssertionError("full listing usato")
        ):
            event = store.get_event("motion_event_20260417_120101")
        self.assertIsNotNone(event)
        self.assertEqual(event["id"], "motion_event_20260417_120101")
        self.assertIn("frames", event)

    def test_get_event_unknown_id_returns_none(self):
        self._make_event("motion_event_20260417_120001")
        store = self._store()
        self.assertIsNone(store.get_event("motion_event_20990101_000000"))


if __name__ == "__main__":
    unittest.main()
