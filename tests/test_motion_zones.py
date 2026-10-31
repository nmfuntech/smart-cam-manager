import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
from flask import Flask

from blackframe import motion_zones
from blackframe.auth import auth_bp, configure_auth, rate_limiter
from blackframe.routes.settings import settings_bp

SQUARE = [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]]


class ZoneStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"MOTION_ZONES_PATH": str(Path(self.tmp.name) / "zones.json")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_and_clear(self):
        motion_zones.set_zones("cam", [SQUARE])
        self.assertEqual(motion_zones.get_zones("cam"), {"exclude": [SQUARE]})
        motion_zones.set_zones("cam", [])
        self.assertEqual(motion_zones.get_zones("cam"), {"exclude": []})

    def test_validation(self):
        for bad in ("x", [[[0, 0], [1, 1]]], [[[0, 0], [2, 0], [0, 1]]], [[["a", 0]] * 3]):
            with self.assertRaises(ValueError):
                motion_zones.validate_polygons(bad)
        with self.assertRaises(ValueError):
            motion_zones.validate_polygons([SQUARE] * (motion_zones.MAX_POLYGONS + 1))

    def test_mask_blocks_polygon_area(self):
        mask = motion_zones.build_mask({"exclude": [SQUARE]}, 100, 100)
        self.assertEqual(mask[10, 10], 0)
        self.assertEqual(mask[90, 90], 255)
        self.assertAlmostEqual(cv2.countNonZero(mask) / 10000, 0.75, delta=0.02)
        self.assertIsNone(motion_zones.build_mask({"exclude": []}, 100, 100))

    def test_cache_reloads_after_file_change(self):
        cache = motion_zones.ZoneMaskCache("cam", check_interval_sec=0)
        mask, area = cache.get((100, 100), now=0)
        self.assertIsNone(mask)
        self.assertEqual(area, 10000)
        motion_zones.set_zones("cam", [SQUARE])
        path = Path(os.environ["MOTION_ZONES_PATH"])
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        mask, area = cache.get((100, 100), now=1)
        self.assertIsNotNone(mask)
        self.assertLess(area, 8000)


class DetectorZoneIntegrationTests(unittest.TestCase):
    def test_motion_inside_excluded_zone_is_ignored(self):
        from blackframe.app import MotionDetector

        detector = MotionDetector.__new__(MotionDetector)
        detector.config = {"min_area": 50, "max_area_ratio": 1.0, "global_change_ratio": 0.0,
                           "morph_dilate_iter": 0}
        detector._kernel_open = np.ones((1, 1), np.uint8)
        detector._kernel_dilate = np.ones((1, 1), np.uint8)
        detector._preprocess = lambda frame: frame
        detector._current_learning_rate = lambda: 0.0
        foreground = np.zeros((100, 100), np.uint8)
        foreground[10:30, 10:30] = 255  # inside the excluded top-left square
        detector._bg_subtractor = SimpleNamespace(apply=lambda img, learningRate: foreground)
        zone_mask = motion_zones.build_mask({"exclude": [SQUARE]}, 100, 100)
        detector._zones = SimpleNamespace(get=lambda shape, now: (zone_mask, 7500.0))
        motion_now, area, frame_area = detector._detect_motion(foreground)
        self.assertFalse(motion_now)
        self.assertEqual(frame_area, 7500.0)

        foreground[:] = 0
        foreground[70:90, 70:90] = 255  # watched area
        motion_now, area, _ = detector._detect_motion(foreground)
        self.assertTrue(motion_now)


class ZoneRouteTests(unittest.TestCase):
    def setUp(self):
        rate_limiter._events.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {
                "APP_ADMIN_PASSWORD": "test-pass",
                "APP_SECRET_KEY": "test-secret",
                "MOTION_ZONES_PATH": str(Path(self.tmp.name) / "zones.json"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        profiles = SimpleNamespace(get_active_profile_id=lambda: "cam-1")
        app = Flask(__name__)
        configure_auth(app)
        app.config["services"] = SimpleNamespace(features=SimpleNamespace(camera_profiles=profiles))
        app.register_blueprint(auth_bp)
        app.register_blueprint(settings_bp)
        self.client = app.test_client()

    def _login(self):
        with self.client.session_transaction() as session:
            session["blackframe_auth_user"] = "admin"
            session["blackframe_csrf_token"] = "tok"

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/api/motion_zones").status_code, 401)

    def test_put_requires_csrf_and_saves(self):
        self._login()
        body = json.dumps({"exclude": [SQUARE]})
        denied = self.client.put("/api/motion_zones", data=body, content_type="application/json")
        self.assertIn(denied.status_code, (400, 403))
        ok = self.client.put(
            "/api/motion_zones",
            data=body,
            content_type="application/json",
            headers={"X-CSRF-Token": "tok"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.client.get("/api/motion_zones").get_json()["exclude"], [SQUARE])

    def test_invalid_zone_rejected(self):
        self._login()
        bad = self.client.put(
            "/api/motion_zones",
            data=json.dumps({"exclude": [[[0, 0], [1, 1]]]}),
            content_type="application/json",
            headers={"X-CSRF-Token": "tok"},
        )
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
