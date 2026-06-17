import importlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from runtime_config import RuntimeConfigManager


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
                import app

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

    def _detector(self, status, *, prefer_video=False):
        class FakeStore:
            current_event_dir = None

            def event_has_classification(self, event_id):
                return False

            def save_event_meta(self, event_id, data):
                pass

        class FakeClassifier:
            enabled = True
            sample_policy = "event_cover"

            def classify(self, frame):
                return {"class_label": "persona", "classification_status": status}

        class FakeRecorder:
            enabled = True

        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"notify_prefer_video": prefer_video}
        detector.recorder = FakeRecorder() if prefer_video else None
        detector._last_motion_rect_norm = None
        detector.event_store = FakeStore()
        detector.classifier = FakeClassifier()
        detector._classified_events = set()
        detector.notified = []
        detector._notify_event = lambda event_id, result: detector.notified.append(event_id)
        return detector

    def test_ok_status_notifies_with_photo_when_no_video(self):
        detector = self._detector("ok")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, ["ev1"])

    def test_unavailable_status_notifies_as_fallback(self):
        detector = self._detector("unavailable")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, ["ev1"])

    def test_ok_status_defers_photo_when_video_preferred(self):
        # With a clip preferred, the snapshot must NOT fire here; the video
        # notification on event close delivers the alert (with the same label).
        detector = self._detector("ok", prefer_video=True)
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])

    def test_ignored_status_does_not_notify(self):
        detector = self._detector("ignored")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])

    def test_no_detection_status_does_not_notify(self):
        detector = self._detector("no_detection")
        detector._classify_event_frame("ev1", object())
        self.assertEqual(detector.notified, [])


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
        self.assertIn('id="cfg-classification-enabled"', page)
        self.assertIn('id="cfg-classification-backend"', page)
        self.assertIn('id="cfg-classification-min-confidence"', page)
        self.assertIn('id="cfg-classification-sample-policy"', page)

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
        training_response = client.get("/model-training")

        self.assertEqual(manager_response.status_code, 200)
        self.assertEqual(activate_response.status_code, 200)
        self.assertEqual(viewer_response.status_code, 200)
        self.assertEqual(training_response.status_code, 200)
        self.assertIn("Addestramento modello", training_response.data.decode("utf-8"))
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
        import auth

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
            import auth

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

    def test_discover_uses_body_token(self):
        client, csrf = self._build_client([])
        with mock.patch(
            "routes.motion.discover_telegram_chats",
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


if __name__ == "__main__":
    unittest.main()
