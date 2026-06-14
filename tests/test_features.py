import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from motion_events import MotionEventStore
from notifications import TelegramNotifier
from recording import EventRecorder
from telegram_commands import TelegramCommandBot


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


class TelegramCommandBotTests(unittest.TestCase):
    def _bot(self, **env):
        base = {
            "TELEGRAM_COMMANDS_ENABLED": "true",
            "NOTIFY_TELEGRAM_BOT_TOKEN": "token",
            "NOTIFY_TELEGRAM_CHAT_ID": "123",
            "TELEGRAM_COMMANDS_RATE_LIMIT_PER_MIN": "20",
        }
        base.update(env)
        self._patch = mock.patch.dict(os.environ, base, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        class FakeCamera:
            def __init__(self):
                self.updates = []

            def get_status(self):
                return {"connected": True, "connection_state": "online", "error": ""}

            def get_frame(self):
                return b"jpeg"

            def apply_runtime_config(self, updates):
                self.updates.append(dict(updates))

        class FakePtz:
            def __init__(self):
                self.updates = []
                self.moves = []

            def get_status(self):
                return {"available": True, "error": "", "host": "127.0.0.1", "port": 2020}

            def move(self, direction):
                self.moves.append(direction)
                return True, ""

            def stop(self):
                self.moves.append("stop")
                return True, ""

            def home(self):
                self.moves.append("home")
                return True, ""

            def apply_runtime_config(self, updates):
                self.updates.append(dict(updates))

        class FakeMotion:
            def __init__(self):
                self.config = {"continuous_record_enabled": False}
                self.updates = []

            def get_status(self):
                return {
                    "enabled": True,
                    "motion_detected": False,
                    "last_motion_at": "2026-01-01 12:00:00",
                }

            def list_events(self, limit=5):
                return []

            def apply_runtime_config(self, updates):
                self.updates.append(dict(updates))
                if "CONTINUOUS_RECORD_ENABLED" in updates:
                    self.config["continuous_record_enabled"] = bool(
                        updates["CONTINUOUS_RECORD_ENABLED"]
                    )

        class FakeRuntimeConfig:
            def __init__(self):
                self.updates = []

            def update(self, updates, allow_sensitive=False, allow_internal=False):
                self.updates.append(dict(updates))
                return {}

        class FakeContinuous:
            def __init__(self):
                self.configs = []

            def status(self):
                return {"enabled": False, "active": False}

            def apply_config(self, config):
                self.configs.append(dict(config))

        services = SimpleNamespace(
            camera=FakeCamera(),
            ptz=FakePtz(),
            motion=FakeMotion(),
            runtime_config=FakeRuntimeConfig(),
            continuous=FakeContinuous(),
        )
        bot = TelegramCommandBot(services)
        self.messages = []
        self.photos = []
        bot._send_message = lambda chat_id, text: self.messages.append((chat_id, text)) or (
            True,
            None,
        )
        bot._send_photo_bytes = (
            lambda chat_id, photo, caption: self.photos.append((chat_id, photo, caption))
            or (True, None)
        )
        return bot, services

    def _handle(self, bot, text: str, chat_id: int = 123):
        bot._handle_update({"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}})

    def test_status_command_accepts_bot_mention(self):
        bot, _ = self._bot()

        self._handle(bot, "/status@BlackframeBot")

        self.assertEqual(self.messages[0][0], "123")
        self.assertIn("Stream: online", self.messages[0][1])

    def test_unauthorized_chat_is_ignored(self):
        bot, services = self._bot()

        self._handle(bot, "/motion_off", chat_id=999)

        self.assertEqual(self.messages, [])
        self.assertEqual(services.runtime_config.updates, [])

    def test_motion_command_updates_runtime_and_services(self):
        bot, services = self._bot()

        self._handle(bot, "/motion_off")

        self.assertEqual(services.runtime_config.updates, [{"MOTION_ENABLED": False}])
        self.assertEqual(services.motion.updates, [{"MOTION_ENABLED": False}])
        self.assertIn("disattivato", self.messages[0][1])

    def test_continuous_command_applies_recorder_config(self):
        bot, services = self._bot()

        self._handle(bot, "/continuous_on")

        self.assertEqual(
            services.runtime_config.updates,
            [{"CONTINUOUS_RECORD_ENABLED": True}],
        )
        self.assertEqual(services.continuous.configs[-1]["continuous_record_enabled"], True)

    def test_snapshot_command_sends_current_frame(self):
        bot, _ = self._bot()

        self._handle(bot, "/snapshot")

        self.assertEqual(self.photos, [("123", b"jpeg", "Snapshot live BLACKFRAME")])
        self.assertEqual(self.messages, [])


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
