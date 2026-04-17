import importlib
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from runtime_config import RuntimeConfigManager


def load_app_module():
    env = {
        "TAPO_USERNAME": "user",
        "TAPO_PASSWORD": "pass",
        "TAPO_HOST": "127.0.0.1",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch("threading.Thread.start", lambda self: None):
            import app

            return importlib.reload(app)


class MotionDetectorEventTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()
        self.tmpdir = tempfile.mkdtemp(prefix="motion-events-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def build_detector(self, event_gap=3.0):
        detector = self.app_module.MotionDetector.__new__(self.app_module.MotionDetector)
        detector.config = {"save_dir": self.tmpdir, "event_gap": event_gap}
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
        self.create_file("motion_event_20260417_120101/cover.jpg")
        self.create_file("motion_event_20260417_120101/latest.jpg")
        self.create_file("motion_event_20260417_120101/frame_20260417_120101_001.jpg")

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


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.app_module = load_app_module()

    def test_even_blur_size_becomes_odd(self):
        with mock.patch.dict(os.environ, {"MOTION_BLUR_SIZE": "30"}, clear=False):
            config = self.app_module.get_motion_config()

        self.assertEqual(config["blur_size"], 31)


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

            def update(self, updates):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=self.app_module.FeatureServices(
                    presets=self.app_module.PresetService("data/test-presets.json"),
                    notifications=self.app_module.NotificationService(),
                    recording=self.app_module.RecordingService("captures/test-recordings"),
                ),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()

        motion_response = client.get("/motion_captures?limit=1")
        stream_response = client.get("/stream_status")

        self.assertEqual(motion_response.status_code, 200)
        self.assertEqual(motion_response.get_json()["total"], 1)
        self.assertEqual(stream_response.status_code, 200)
        self.assertTrue(stream_response.get_json()["connected"])

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

            def update(self, updates):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        app = self.app_module.create_app(
            self.app_module.AppServices(
                camera=FakeCamera(),
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=self.app_module.FeatureServices(
                    presets=self.app_module.PresetService("data/test-presets.json"),
                    notifications=self.app_module.NotificationService(),
                    recording=self.app_module.RecordingService("captures/test-recordings"),
                ),
                runtime_config=FakeRuntimeConfig(),
            )
        )
        client = app.test_client()
        response = client.get("/")

        self.assertEqual(response.status_code, 200)

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
                self.current = {"MOTION_ENABLED": True, "MOTION_THRESHOLD": 35}

            def get_public_config(self):
                return self.current

            def update(self, updates):
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
                features=self.app_module.FeatureServices(
                    presets=self.app_module.PresetService("data/test-presets.json"),
                    notifications=self.app_module.NotificationService(),
                    recording=self.app_module.RecordingService("captures/test-recordings"),
                ),
                runtime_config=fake_runtime,
            )
        )
        client = app.test_client()

        get_response = client.get("/runtime_config")
        patch_response = client.patch(
            "/api/runtime_config",
            json={"updates": {"MOTION_ENABLED": False, "MOTION_THRESHOLD": 55}},
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json()["config"]["MOTION_ENABLED"], True)
        self.assertEqual(patch_response.status_code, 200)
        self.assertEqual(patch_response.get_json()["config"]["MOTION_ENABLED"], False)
        self.assertEqual(fake_camera.last_updates["MOTION_ENABLED"], False)
        self.assertEqual(fake_ptz.last_updates["MOTION_THRESHOLD"], 55)
        self.assertEqual(fake_motion.last_updates["MOTION_THRESHOLD"], 55)


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

            def update(self, updates):
                return {"MOTION_ENABLED": updates.get("MOTION_ENABLED", True)}

        return self.app_module.create_app(
            self.app_module.AppServices(
                camera=camera,
                ptz=FakePtz(),
                motion=FakeMotion(),
                features=self.app_module.FeatureServices(
                    presets=self.app_module.PresetService("data/test-presets.json"),
                    notifications=self.app_module.NotificationService(),
                    recording=self.app_module.RecordingService("captures/test-recordings"),
                ),
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
                    "MOTION_THRESHOLD=35",
                    "MOTION_BLUR_SIZE=30",
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
                "MOTION_THRESHOLD": "51",
                "MOTION_BLUR_SIZE": "30",
            }
        )
        env_text = self.env_path.read_text(encoding="utf-8")

        self.assertEqual(config["MOTION_ENABLED"], False)
        self.assertEqual(config["MOTION_THRESHOLD"], 51)
        self.assertEqual(config["MOTION_BLUR_SIZE"], 31)
        self.assertIn("MOTION_ENABLED=false", env_text)
        self.assertIn("MOTION_THRESHOLD=51", env_text)
        self.assertIn("MOTION_BLUR_SIZE=31", env_text)

    def test_update_rejects_unknown_key(self):
        manager = RuntimeConfigManager(self.env_path)
        with self.assertRaises(ValueError):
            manager.update({"TAPO_PASSWORD": "new-pass"})


if __name__ == "__main__":
    unittest.main()
