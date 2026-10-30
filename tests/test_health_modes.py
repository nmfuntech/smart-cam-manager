import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from blackframe import audit, health, modes
from blackframe.health import HealthMonitor
from blackframe.modes import ModeManager, parse_schedule
from blackframe.timewindows import in_window, parse_window


class TimeWindowTests(unittest.TestCase):
    def test_wrapping_window(self):
        window = parse_window("23:00-07:00")
        self.assertTrue(in_window(window, datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(in_window(window, datetime(2026, 1, 1, 6, 59)))
        self.assertFalse(in_window(window, datetime(2026, 1, 1, 12, 0)))

    def test_invalid_windows(self):
        for raw in ("", "x", "25:00-07:00", "07:00-07:00"):
            self.assertIsNone(parse_window(raw))


class ModeManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mode.json"

    def test_default_is_armed_and_state_persists(self):
        manager = ModeManager(self.path)
        self.assertEqual(manager.mode, "armato")
        self.assertTrue(manager.set_mode("casa"))
        self.assertFalse(manager.set_mode("home"))  # alias, already there
        self.assertEqual(ModeManager(self.path).mode, "casa")

    def test_gates(self):
        manager = ModeManager(self.path)
        manager.set_mode("casa")
        self.assertTrue(manager.allows_detection())
        self.assertFalse(manager.allows_alerts())
        manager.set_mode("disarmato")
        self.assertFalse(manager.allows_detection())

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            ModeManager(self.path).set_mode("vacanza")

    def test_schedule_parse_skips_bad_entries(self):
        self.assertEqual(
            parse_schedule("08:30=armato, 19:00=casa, 99:00=armato, 10:00=boh"),
            [(510, "armato"), (1140, "casa")],
        )

    def test_schedule_applies_once_per_minute_and_day(self):
        changes = []
        manager = ModeManager(self.path, on_change=lambda a, b, by: changes.append((b, by)))
        with mock.patch.dict(os.environ, {"MODE_SCHEDULE": "19:00=casa"}):
            self.assertEqual(manager.apply_schedule(datetime(2026, 1, 1, 19, 0, 5)), "casa")
            manager.set_mode("armato")  # manual override
            self.assertIsNone(manager.apply_schedule(datetime(2026, 1, 1, 19, 0, 40)))
            self.assertIsNone(manager.apply_schedule(datetime(2026, 1, 1, 19, 1)))
        self.assertEqual(manager.mode, "armato")
        self.assertEqual(changes[0], ("casa", "programmazione"))


class AuditTests(unittest.TestCase):
    def test_record_and_format(self):
        audit.record("telegram", "admin:1", "motion_off")
        audit.record("telegram", "admin:1", "device_on", "lampada", ok=False)
        entries = audit.recent(5)
        self.assertEqual(entries[0]["action"], "device_on")
        text = audit.format_recent()
        self.assertIn("motion_off", text)
        self.assertIn("⚠️", text)

    def test_rotation(self):
        with mock.patch.dict(os.environ, {"AUDIT_MAX_KB": "16"}):
            for _ in range(400):
                audit.record("telegram", "admin:1", "x" * 40)
        path = Path(os.environ["AUDIT_LOG_PATH"])
        self.assertTrue(path.with_name(path.name + ".1").exists())


class FakeCamera:
    def __init__(self):
        self.state = "online"

    def get_status(self):
        return {"connection_state": self.state, "error": "timeout RTSP"}


class FakeMotion:
    def __init__(self, save_dir):
        self.config = {"enabled": True, "save_dir": save_dir}
        self.processed_frames = 0
        self.classifier = None


class HealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.camera = FakeCamera()
        self.motion = FakeMotion(self.tmp.name)
        services = SimpleNamespace(camera=self.camera, motion=self.motion, monitors={})
        env = {
            "NOTIFY_TELEGRAM_BOT_TOKEN": "t",
            "NOTIFY_TELEGRAM_CHAT_ID": "1",
            "HEALTH_CAMERA_OFFLINE_MIN": "1",
            "HEALTH_MOTION_STALL_SEC": "60",
            "DIGEST_TIME": "",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        modes.set_mode_manager(ModeManager(Path(self.tmp.name) / "mode.json"))
        self.sent = []
        send_patch = mock.patch.object(
            health.telegram_client,
            "send_message",
            side_effect=lambda token, chat, text, **kw: self.sent.append(text) or {"ok": True},
        )
        send_patch.start()
        self.addCleanup(send_patch.stop)
        self.monitor = HealthMonitor(services)

    def test_offline_alert_after_threshold_then_recovery(self):
        self.camera.state = "offline"
        self.monitor.check_once(now=0)
        self.monitor.check_once(now=30)
        self.assertEqual(self.sent, [])
        self.monitor.check_once(now=61)
        self.assertIn("offline", self.sent[0])
        self.monitor.check_once(now=90)
        self.assertEqual(len(self.sent), 1)  # alerted once
        self.camera.state = "online"
        self.motion.processed_frames = 5
        self.monitor.check_once(now=200)
        self.assertIn("di nuovo online", self.sent[1])

    def test_short_glitch_is_not_reported(self):
        self.camera.state = "offline"
        self.monitor.check_once(now=0)
        self.camera.state = "online"
        self.monitor.check_once(now=20)
        self.assertEqual(self.sent, [])

    def test_motion_stall_detected_and_cleared(self):
        self.motion.processed_frames = 10
        self.monitor.check_once(now=0)
        self.monitor.check_once(now=61)
        self.assertIn("bloccato", self.sent[0])
        self.motion.processed_frames = 11
        self.monitor.check_once(now=70)
        self.assertIn("ripartito", self.sent[1])

    def test_no_stall_alert_when_disarmed(self):
        modes.get_mode_manager().set_mode("disarmato")
        self.motion.processed_frames = 10
        self.monitor.check_once(now=0)
        self.monitor.check_once(now=500)
        self.assertEqual(self.sent, [])

    def test_undelivered_alert_is_retried(self):
        results = [{"ok": False, "description": "rete"}, {"ok": True}]
        with mock.patch.object(
            health.telegram_client, "send_message", side_effect=lambda *a, **k: results.pop(0)
        ):
            self.assertFalse(self.monitor.send("test"))
            self.monitor._flush_outbox()
        self.assertEqual(len(self.monitor._outbox), 0)

    def test_digest_counts_events_and_false_alarms(self):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = Path(self.tmp.name)
        (root / f"motion_event_{stamp}__persona").mkdir()
        fa = root / f"motion_event_{stamp[:-1]}0__animale_domestico"
        fa.mkdir()
        (fa / "meta.json").write_text(json.dumps({"feedback": {"false_alarm": True}}))
        text = self.monitor.build_digest()
        self.assertIn("Eventi: 2", text)
        self.assertIn("falsi allarmi: 1", text)

    def test_digest_sent_once_per_day_and_not_stale(self):
        with mock.patch.dict(os.environ, {"DIGEST_TIME": "21:00"}):
            self.monitor._maybe_send_digest(datetime(2026, 1, 1, 20, 59))
            self.monitor._maybe_send_digest(datetime(2026, 1, 1, 21, 0))
            self.monitor._maybe_send_digest(datetime(2026, 1, 1, 21, 30))
            self.monitor._maybe_send_digest(datetime(2026, 1, 2, 23, 30))  # >2h late
        self.assertEqual(len(self.sent), 1)


class NotifierExtrasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {
            "NOTIFY_TELEGRAM_ENABLED": "true",
            "NOTIFY_TELEGRAM_BOT_TOKEN": "t",
            "NOTIFY_TELEGRAM_CHAT_ID": "1",
            "TELEGRAM_COMMANDS_ENABLED": "true",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        from blackframe.notifications import TelegramNotifier

        self.notifier_cls = TelegramNotifier
        self.queue = Path(self.tmp.name) / ".telegram_queue.json"

    def test_mute_survives_restart(self):
        notifier = self.notifier_cls(queue_path=self.queue)
        notifier.mute(600)
        reloaded = self.notifier_cls(queue_path=self.queue)
        self.assertGreater(reloaded.muted_remaining(), 500)
        reloaded.mute(0)
        self.assertEqual(self.notifier_cls(queue_path=self.queue).muted_remaining(), 0)

    def test_alert_buttons_include_false_alarm_for_event(self):
        notifier = self.notifier_cls(queue_path=self.queue)
        markup = notifier._alert_markup("motion_event_20261027_232019__persona")
        data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn("/false_alarm 20261027_232019", data)
        self.assertIn("/snapshot", data)

    def test_quiet_hours_never_silence_people_by_default(self):
        notifier = self.notifier_cls(queue_path=self.queue)
        with mock.patch.dict(os.environ, {"NOTIFY_QUIET_HOURS": "00:00-23:59"}):
            self.assertFalse(notifier._is_quiet("persona"))
            self.assertTrue(notifier._is_quiet("animale_domestico"))
            self.assertTrue(notifier._is_quiet(None))

    def test_alerts_suppressed_in_home_mode(self):
        modes.set_mode_manager(ModeManager(Path(self.tmp.name) / "mode.json"))
        modes.get_mode_manager().set_mode("casa")
        notifier = self.notifier_cls(queue_path=self.queue)
        self.assertFalse(notifier.notify_event("motion_event_1", class_label="persona"))

    def test_video_retried_before_cover_fallback(self):
        notifier = self.notifier_cls(queue_path=self.queue)
        event_dir = Path(self.tmp.name) / "ev"
        event_dir.mkdir()
        video = event_dir / "event.mp4"
        video.write_bytes(b"v")
        (event_dir / "cover.jpg").write_bytes(b"c")
        notifier._send_video = lambda *a: False
        photos = []
        notifier._send = lambda *a: photos.append(a[-1]) or True
        from blackframe.notifications import _DeliveryJob

        job = _DeliveryJob("e", None, None, str(video), "t", "1", attempts=0)
        self.assertFalse(notifier._deliver(job))  # retry the video first
        self.assertEqual(photos, [])
        last = _DeliveryJob("e", None, None, str(video), "t", "1", attempts=3)
        self.assertTrue(notifier._deliver(last))  # last attempt: cover photo
        self.assertTrue(photos[0].endswith("cover.jpg"))


class FalseAlarmTests(unittest.TestCase):
    def test_store_marks_renamed_event(self):
        from blackframe.motion_events import MotionEventStore

        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "motion_event_20261027_232019__persona"
            event.mkdir()
            store = MotionEventStore({"save_dir": tmp})
            self.assertTrue(store.mark_false_alarm("20261027_232019"))
            meta = json.loads((event / "meta.json").read_text())
            self.assertTrue(meta["feedback"]["false_alarm"])
            self.assertFalse(store.mark_false_alarm("20200101_000000"))

    def test_registry_command_validates_and_reports(self):
        from blackframe.commands import execute

        calls = []
        motion = SimpleNamespace(mark_false_alarm=lambda base: calls.append(base) or True)
        services = SimpleNamespace(motion=motion, monitors={})
        self.assertIn("Uso", execute("false_alarm", "boh", services).text)
        self.assertIn("falso allarme", execute("false_alarm", "20261027_232019", services).text)
        self.assertEqual(calls, ["20261027_232019"])


if __name__ == "__main__":
    unittest.main()
