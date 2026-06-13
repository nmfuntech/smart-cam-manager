import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from motion_events import MotionEventStore
from notifications import TelegramNotifier
from recording import EventRecorder


def make_store(save_dir, **overrides):
    config = {
        "save_frames": True,
        "save_dir": str(save_dir),
        "event_gap": 30.0,
        "max_event_duration": 45.0,
    }
    config.update(overrides)
    return MotionEventStore(config)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="retention-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _event(self, name: str, size_bytes: int = 16):
        event_dir = self.tmpdir / name
        event_dir.mkdir(parents=True, exist_ok=True)
        (event_dir / "cover.jpg").write_bytes(b"x" * size_bytes)
        return event_dir

    def test_purge_removes_events_older_than_max_age(self):
        old = self._event("motion_event_20200101_120000")
        recent = self._event(f"motion_event_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        store = make_store(self.tmpdir)

        removed = store.purge_old_events(max_age_days=1, max_total_mb=0)

        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_purge_enforces_size_quota_oldest_first(self):
        one_mb = 1024 * 1024
        oldest = self._event("motion_event_20260101_120001", size_bytes=one_mb)
        middle = self._event("motion_event_20260101_120002", size_bytes=one_mb)
        newest = self._event("motion_event_20260101_120003", size_bytes=one_mb)
        store = make_store(self.tmpdir)

        removed = store.purge_old_events(max_age_days=0, max_total_mb=2)

        self.assertEqual(removed, 1)
        self.assertFalse(oldest.exists())
        self.assertTrue(middle.exists())
        self.assertTrue(newest.exists())

    def test_purge_never_deletes_the_open_event(self):
        current = self._event("motion_event_20200101_120000")
        store = make_store(self.tmpdir)
        store.current_event_dir = current

        removed = store.purge_old_events(max_age_days=1, max_total_mb=0)

        self.assertEqual(removed, 0)
        self.assertTrue(current.exists())


class ClassificationDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dedup-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_event_has_classification_reads_disk(self):
        store = make_store(self.tmpdir)
        event_id = "motion_event_20260101_120000"
        (self.tmpdir / event_id).mkdir(parents=True, exist_ok=True)

        self.assertFalse(store.event_has_classification(event_id))

        store.save_event_meta(event_id, {"classification": {"class_label": "persona"}})

        self.assertTrue(store.event_has_classification(event_id))


class TelegramNotifierTests(unittest.TestCase):
    def _notifier(self, **env):
        base = {
            "NOTIFY_TELEGRAM_ENABLED": "true",
            "NOTIFY_TELEGRAM_BOT_TOKEN": "token",
            "NOTIFY_TELEGRAM_CHAT_ID": "123",
            "NOTIFY_ON_CLASSES": "",
            "NOTIFY_MIN_INTERVAL_SEC": "0",
        }
        base.update(env)
        self._patch = mock.patch.dict(os.environ, base, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        notifier = TelegramNotifier()
        self.sent = []
        notifier._send = lambda *a, **k: self.sent.append(a)
        return notifier

    def test_disabled_notifier_does_not_send(self):
        notifier = self._notifier(NOTIFY_TELEGRAM_ENABLED="false")
        self.assertFalse(notifier.notify_event("motion_event_1", class_label="persona"))

    def test_missing_token_does_not_send(self):
        notifier = self._notifier(NOTIFY_TELEGRAM_BOT_TOKEN="")
        self.assertFalse(notifier.notify_event("motion_event_1", class_label="persona"))

    def test_class_filter_blocks_unlisted_class(self):
        notifier = self._notifier(NOTIFY_ON_CLASSES="persona")
        self.assertFalse(notifier.notify_event("motion_event_1", class_label="animale_domestico"))
        self.assertTrue(notifier.notify_event("motion_event_2", class_label="persona"))

    def test_cooldown_blocks_rapid_second_event(self):
        notifier = self._notifier(NOTIFY_MIN_INTERVAL_SEC="3600")
        self.assertTrue(notifier.notify_event("motion_event_1", class_label="persona"))
        self.assertFalse(notifier.notify_event("motion_event_2", class_label="persona"))


class RecordingConfigTests(unittest.TestCase):
    def test_recorder_disabled_is_noop(self):
        recorder = EventRecorder(camera_stream=None, config={"record_enabled": False})
        self.assertFalse(recorder.enabled)
        # Must not raise or start a thread when disabled.
        recorder.start_event("/tmp/does-not-matter")
        self.assertIsNone(recorder._thread)

    def test_recorder_enabled_flag_reads_config(self):
        recorder = EventRecorder(camera_stream=None, config={"record_enabled": True})
        self.assertTrue(recorder.enabled)


if __name__ == "__main__":
    unittest.main()
