import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from blackframe.motion_events import MotionEventStore
from blackframe.notifications import TelegramNotifier, _DeliveryJob
from blackframe.recording import EventRecorder
from blackframe.telegram_commands import TelegramCommandBot


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
        queue_dir = Path(tempfile.mkdtemp(prefix="tg-queue-"))
        self.addCleanup(shutil.rmtree, queue_dir, ignore_errors=True)
        notifier = TelegramNotifier(queue_path=queue_dir / ".telegram_queue.json")
        self.sent = []
        self.delivered: list[str] = []

        def _send(*args, **kwargs):
            self.sent.append(("send", args))
            return True

        def _send_video(*args, **kwargs):
            self.sent.append(("video", args))
            return True

        notifier._send = _send
        notifier._send_video = _send_video
        return notifier

    def _wait_deliveries(self, count: int, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while len(self.sent) < count and time.time() < deadline:
            time.sleep(0.05)

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

    def test_cooldown_queues_second_event(self):
        notifier = self._notifier(NOTIFY_MIN_INTERVAL_SEC="0.05")
        self.assertTrue(notifier.notify_event("motion_event_1", class_label="persona"))
        self.assertTrue(notifier.notify_event("motion_event_2", class_label="persona"))
        self._wait_deliveries(2)
        self.assertEqual(len(self.sent), 2)

    def test_notification_queue_is_bounded(self):
        notifier = self._notifier(NOTIFY_QUEUE_MAX="2")
        notifier._start_worker = lambda: None
        for index in range(3):
            self.assertTrue(notifier.notify_event(f"motion_event_{index}", class_label="persona"))
        self.assertEqual(len(notifier._pending), 2)
        self.assertEqual(
            [job.event_id for job in notifier._pending],
            ["motion_event_1", "motion_event_2"],
        )

    def test_on_delivered_only_after_successful_send(self):
        delivered: list[str] = []

        notifier_fail = self._notifier()
        notifier_fail._send = lambda *a, **k: False
        self.assertTrue(
            notifier_fail.notify_event(
                "motion_event_fail",
                class_label="persona",
                on_delivered=lambda: delivered.append("fail"),
            )
        )
        time.sleep(0.5)
        self.assertEqual(delivered, [])

        notifier_ok = self._notifier()
        self.assertTrue(
            notifier_ok.notify_event(
                "motion_event_ok",
                class_label="persona",
                on_delivered=lambda: delivered.append("ok"),
            )
        )
        self._wait_deliveries(1)
        self.assertEqual(delivered, ["ok"])

    def test_failed_send_is_retried_then_delivered(self):
        notifier = self._notifier()
        attempts = {"n": 0}

        def _send(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return False  # primo tentativo: blip di rete
            self.sent.append(("send", args))
            return True

        notifier._send = _send
        with mock.patch("blackframe.notifications._RETRY_BACKOFF_BASE_SEC", 0.05):
            self.assertTrue(notifier.notify_event("motion_event_retry", class_label="persona"))
            self._wait_deliveries(1, timeout=3.0)
        self.assertEqual(len(self.sent), 1)
        self.assertGreaterEqual(attempts["n"], 2)

    def test_dropped_after_max_attempts(self):
        notifier = self._notifier(NOTIFY_MAX_ATTEMPTS="2")
        calls = {"n": 0}
        notifier._send = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or False
        with mock.patch("blackframe.notifications._RETRY_BACKOFF_BASE_SEC", 0.05):
            notifier.notify_event("motion_event_drop", class_label="persona")
            deadline = time.time() + 3.0
            while calls["n"] < 2 and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)  # garantisce che non ci sia un terzo tentativo
        self.assertEqual(calls["n"], 2)
        with notifier._lock:
            self.assertEqual(notifier._pending, [])

    def test_queue_persisted_and_reloaded(self):
        notifier = self._notifier()
        job = _DeliveryJob(
            event_id="motion_event_persist",
            class_label="persona",
            image_path=None,
            video_path=None,
            token="token",
            chat_id="123",
        )
        with notifier._lock:
            notifier._pending.append(job)
            notifier._persist_locked()
        self.assertTrue(notifier._queue_path.exists())
        # I segreti non finiscono nel file persistito.
        self.assertNotIn("token", notifier._queue_path.read_text())

        # Worker disattivato durante la ricostruzione: _pending resta stabile per l'asserzione
        # (altrimenti il worker potrebbe drenare il job prima del check).
        with mock.patch.object(TelegramNotifier, "_start_worker", lambda self: None):
            restored = TelegramNotifier(queue_path=notifier._queue_path)
        names = [j.event_id for j in restored._pending]
        self.assertIn("motion_event_persist", names)
        # token/chat_id riattaccati dall'env, non dal file.
        self.assertEqual(restored._pending[0].token, "token")

    def test_mute_blocks_notifications(self):
        notifier = self._notifier()
        notifier.mute(60)
        self.assertFalse(notifier.notify_event("motion_event_1", class_label="persona"))
        notifier.mute(0)
        self.assertTrue(notifier.notify_event("motion_event_2", class_label="persona"))

    def test_mute_expires(self):
        notifier = self._notifier()
        with mock.patch("blackframe.notifications.time.monotonic", return_value=1000.0):
            notifier.mute(60)
        with mock.patch("blackframe.notifications.time.monotonic", return_value=1061.0):
            self.assertTrue(notifier.notify_event("motion_event_1", class_label="persona"))

    def test_caption_includes_person_emoji(self):
        notifier = self._notifier()
        caption = notifier._caption("motion_event_20260617_120000", "persona")
        self.assertIn("🧍", caption)
        self.assertIn("persona", caption)

    def test_caption_includes_dog_emoji(self):
        notifier = self._notifier()
        caption = notifier._caption("motion_event_20260617_120000", "animale_domestico")
        self.assertIn("🐕", caption)

    def test_caption_without_class_has_no_emoji(self):
        notifier = self._notifier()
        caption = notifier._caption("motion_event_20260617_120000", None)
        self.assertNotIn("🧍", caption)
        self.assertNotIn("🐕", caption)


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

        class FakeNotifier:
            def __init__(self):
                self.muted = []

            def mute(self, seconds):
                self.muted.append(seconds)
                return max(0.0, seconds)

            def muted_remaining(self):
                return self.muted[-1] if self.muted else 0.0

        from blackframe.automation.devices import DeviceError, MockDevice

        class FakeRegistry:
            def __init__(self):
                self.devices = {"luce": MockDevice("luce")}

            def device_names(self):
                return list(self.devices)

            def get(self, name):
                if name not in self.devices:
                    raise DeviceError(f"Device '{name}' assente")
                return self.devices[name]

        class FakeEngine:
            def __init__(self):
                self.ran = []

            def run_rule(self, name, *, execute=True):
                if name == "ignota":
                    return None
                self.ran.append((name, execute))
                return [object(), object()]

        services = SimpleNamespace(
            camera=FakeCamera(),
            ptz=FakePtz(),
            motion=FakeMotion(),
            runtime_config=FakeRuntimeConfig(),
            continuous=FakeContinuous(),
            features=SimpleNamespace(telegram=FakeNotifier()),
            automation_registry=FakeRegistry(),
            automation_engine=FakeEngine(),
        )
        services.reloaded = 0

        def _reload():
            services.reloaded += 1

        services.reload_automation = _reload

        def _apply_all(updates):
            services.camera.apply_runtime_config(updates)
            services.ptz.apply_runtime_config(updates)
            services.motion.apply_runtime_config(updates)

        services.apply_runtime_config_all = _apply_all
        bot = TelegramCommandBot(services)
        self.messages = []
        self.photos = []
        self.answers = []
        bot._send_message = lambda chat_id, text, reply_markup=None: self.messages.append(
            (chat_id, text, reply_markup)
        ) or (True, None)
        bot._answer_callback = lambda callback_id, text="": self.answers.append((callback_id, text))
        bot._send_photo_bytes = lambda chat_id, photo, caption: self.photos.append(
            (chat_id, photo, caption)
        ) or (True, None)
        self.videos = []
        bot._send_video_bytes = lambda chat_id, video, caption: self.videos.append(
            (chat_id, video, caption)
        ) or (True, None)
        return bot, services

    def _handle(self, bot, text: str, chat_id: int = 123):
        bot._handle_update({"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}})

    def _callback(self, bot, data: str, chat_id: int = 123):
        bot._handle_callback({"id": "cb1", "data": data, "message": {"chat": {"id": chat_id}}})

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

    def test_guest_can_read_but_cannot_mutate_or_record_clip(self):
        bot, services = self._bot()
        bot._is_admin = lambda _chat_id: False

        readonly = bot._dispatch("/status", [], "guest")
        mutation = bot._dispatch("/motion_off", [], "guest")
        clip = bot._dispatch("/clip", [], "guest")

        self.assertIn("BLACKFRAME", readonly)
        self.assertIn("riservato", mutation)
        self.assertIn("riservato", clip)
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

    def test_notifications_command_toggles_runtime(self):
        bot, services = self._bot()

        self._handle(bot, "/notifications_off")

        self.assertEqual(
            services.runtime_config.updates,
            [{"NOTIFY_TELEGRAM_ENABLED": False}],
        )
        self.assertIn("disattivate", self.messages[0][1])

    def test_classification_command_toggles_runtime(self):
        bot, services = self._bot()

        self._handle(bot, "/classification_on")

        self.assertEqual(
            services.runtime_config.updates,
            [{"CLASSIFICATION_ENABLED": True}],
        )

    def test_sensitivity_preset_sets_threshold(self):
        bot, services = self._bot()

        self._handle(bot, "/sensitivity media")

        self.assertEqual(services.runtime_config.updates, [{"MOTION_THRESHOLD": 30}])
        self.assertEqual(services.motion.updates, [{"MOTION_THRESHOLD": 30}])

    def test_sensitivity_unknown_preset_does_not_update(self):
        bot, services = self._bot()

        self._handle(bot, "/sensitivity turbo")

        self.assertEqual(services.runtime_config.updates, [])
        self.assertIn("sconosciuto", self.messages[0][1])

    def test_mute_command_calls_notifier(self):
        bot, services = self._bot()

        self._handle(bot, "/mute 2")

        self.assertEqual(services.features.telegram.muted, [120.0])
        self.assertIn("silenziate", self.messages[0][1])

    def test_resume_command_clears_mute(self):
        bot, services = self._bot()

        self._handle(bot, "/resume")

        self.assertEqual(services.features.telegram.muted, [0])

    # --- Domotica ----------------------------------------------------------

    def test_devices_command_lists(self):
        bot, _ = self._bot()
        self._handle(bot, "/devices")
        self.assertIn("luce", self.messages[0][1])

    def test_inventory_command_lists_cameras_and_devices(self):
        from blackframe.commands.registry import execute

        _, services = self._bot()
        services.features.camera_profiles = SimpleNamespace(
            list_profiles=lambda: [
                {"id": "front", "name": "Ingresso", "active": True}
            ]
        )

        result = execute("inventory", None, services)

        self.assertIn("Ingresso", result.text)
        self.assertIn("luce", result.text)

    def test_entity_status_reads_device_state(self):
        from blackframe.commands.registry import execute

        _, services = self._bot()
        services.automation_registry.devices["luce"].turn_on()

        result = execute("entity_status", "luce", services)

        self.assertIn("luce", result.text)
        self.assertIn("acceso", result.text.lower())

    def test_device_on_turns_on(self):
        bot, services = self._bot()
        self._handle(bot, "/device_on luce")
        self.assertTrue(services.automation_registry.devices["luce"].is_on)
        self.assertIn("acceso", self.messages[0][1])

    def test_device_off_unknown_reports_error(self):
        bot, _ = self._bot()
        self._handle(bot, "/device_off nope")
        self.assertIn("non trovato", self.messages[0][1])

    def test_device_on_missing_arg(self):
        bot, _ = self._bot()
        self._handle(bot, "/device_on")
        self.assertIn("Uso:", self.messages[0][1])

    def test_rule_run_executes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules_path = Path(tmp.name) / "rules.yaml"
        rules_path.write_text(
            "- name: luce_notte\n  on: person_detected\n"
            "  do:\n    - device: luce\n      action: turn_on\n"
        )
        bot, services = self._bot(AUTOMATION_RULES_PATH=str(rules_path))
        self._handle(bot, "/rule_run luce_notte")
        self.assertEqual(services.automation_engine.ran, [("luce_notte", True)])
        self.assertIn("eseguita", self.messages[0][1])

    def test_rule_run_unknown_returns_not_found(self):
        bot, _ = self._bot()
        self._handle(bot, "/rule_run ignota")
        self.assertIn("non trovata", self.messages[0][1])

    def test_rule_run_without_engine_reports_disabled(self):
        bot, services = self._bot()
        services.automation_engine = None
        self._handle(bot, "/rule_run luce_notte")
        self.assertIn("disabilitata", self.messages[0][1])

    def test_rules_list_and_toggle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules_path = Path(tmp.name) / "rules.yaml"
        rules_path.write_text(
            "- name: luce_notte\n  on: person_detected\n"
            "  do:\n    - device: luce\n      action: turn_on\n"
        )
        bot, services = self._bot(AUTOMATION_RULES_PATH=str(rules_path))

        self._handle(bot, "/rules")
        self.assertIn("luce_notte", self.messages[-1][1])
        self.assertIn("on", self.messages[-1][1])

        self._handle(bot, "/rule_off luce_notte")
        self.assertIn("disabilitata", self.messages[-1][1])
        self.assertEqual(services.reloaded, 1)

        import yaml

        data = yaml.safe_load(rules_path.read_text())
        self.assertFalse(data[0]["enabled"])

    def test_rule_off_unknown_returns_not_found(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rules_path = Path(tmp.name) / "rules.yaml"
        rules_path.write_text("[]\n")
        bot, _ = self._bot(AUTOMATION_RULES_PATH=str(rules_path))
        self._handle(bot, "/rule_off nope")
        self.assertIn("non trovata", self.messages[-1][1])

    def test_config_command_reports_states(self):
        bot, _ = self._bot(MOTION_ENABLED="true", NOTIFY_TELEGRAM_ENABLED="false")

        self._handle(bot, "/config")

        self.assertIn("Impostazioni BLACKFRAME", self.messages[0][1])
        self.assertIn("Notifiche: spente", self.messages[0][1])

    def test_clip_rejects_too_long_duration(self):
        bot, _ = self._bot()

        self._handle(bot, "/clip 99")

        self.assertIn("massima", self.messages[0][1])
        self.assertEqual(self.videos, [])

    def test_clip_invalid_duration_shows_usage(self):
        bot, _ = self._bot()

        self._handle(bot, "/clip abc")

        self.assertIn("Uso", self.messages[0][1])

    def test_clip_concurrency_is_bounded(self):
        bot, services = self._bot()
        services.camera.get_raw_frame = lambda: object()
        self.assertTrue(bot._reserve_clip())
        response = bot._send_clip([], "123")
        self.assertIn("gia in registrazione", response)

    def test_record_and_send_clip_sends_video(self):
        bot, services = self._bot()

        def fake_record(camera, path, seconds, fps=10.0, max_width=0):
            Path(path).write_bytes(b"video-bytes")
            return Path(path)

        with mock.patch("blackframe.telegram_commands.record_clip", fake_record):
            bot._record_and_send_clip("123", 10)

        self.assertEqual(len(self.videos), 1)
        self.assertEqual(self.videos[0][1], b"video-bytes")

    def test_record_and_send_clip_reports_failure(self):
        bot, _ = self._bot()

        with mock.patch("blackframe.telegram_commands.record_clip", lambda *a, **k: None):
            bot._record_and_send_clip("123", 10)

        self.assertEqual(self.videos, [])
        self.assertIn("fallita", self.messages[-1][1])

    def test_reply_button_label_maps_to_command(self):
        bot, _ = self._bot()

        self._handle(bot, "📊 Stato")

        self.assertIn("BLACKFRAME — Stato", self.messages[0][1])

    def test_menu_command_sends_inline_keyboard(self):
        bot, _ = self._bot()

        self._handle(bot, "/menu")

        self.assertIn("inline_keyboard", self.messages[0][2])

    def test_help_command_sends_reply_keyboard(self):
        bot, _ = self._bot()

        self._handle(bot, "/help")

        self.assertIn("keyboard", self.messages[0][2])

    def test_callback_dispatches_command(self):
        bot, services = self._bot()

        self._callback(bot, "/motion_off")

        self.assertEqual(services.runtime_config.updates, [{"MOTION_ENABLED": False}])
        self.assertEqual(self.answers, [("cb1", "")])

    def test_callback_from_unauthorized_chat_ignored(self):
        bot, services = self._bot()

        self._callback(bot, "/motion_off", chat_id=999)

        self.assertEqual(services.runtime_config.updates, [])

    def test_snapshot_command_sends_current_frame(self):
        bot, _ = self._bot()

        self._handle(bot, "/snapshot")

        self.assertEqual(self.photos, [("123", b"jpeg", "Snapshot live BLACKFRAME")])
        self.assertEqual(self.messages, [])

    def test_invite_wrong_code_sends_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            bot, _ = self._bot(TELEGRAM_INVITE_CODE="secret", TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/start wrong", chat_id=999)

            self.assertIn("non valido", self.messages[0][1])
            self.assertFalse(os.path.exists(guests_file))

    def test_invite_no_code_in_env_silently_ignores_start(self):
        bot, _ = self._bot()

        self._handle(bot, "/start secret", chat_id=999)

        self.assertEqual(self.messages, [])

    def test_invite_correct_code_adds_guest_and_welcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            bot, _ = self._bot(TELEGRAM_INVITE_CODE="secret", TELEGRAM_GUESTS_FILE=guests_file)
            update = {
                "update_id": 1,
                "message": {
                    "chat": {"id": 999},
                    "text": "/start secret",
                    "from": {"first_name": "Mario", "last_name": "Rossi"},
                },
            }

            bot._handle_update(update)

            self.assertIn("Benvenuto", self.messages[0][1])
            data = json.loads(Path(guests_file).read_text())
            self.assertIn("999", data)
            self.assertEqual(data["999"]["name"], "Mario Rossi")

    def test_invited_guest_is_limited_to_readonly_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            Path(guests_file).write_text(
                json.dumps({"456": {"name": "Paola", "joined_at": "2026-01-01T00:00:00"}})
            )
            bot, services = self._bot(TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/status", chat_id=456)
            self._handle(bot, "/motion_off", chat_id=456)

            self.assertIn("BLACKFRAME", self.messages[0][1])
            self.assertIn("riservato", self.messages[1][1])
            self.assertEqual(services.runtime_config.updates, [])

    def test_admin_invite_shows_code(self):
        bot, _ = self._bot(TELEGRAM_INVITE_CODE="mysecret")
        bot._bot_username = "BlackframeBot"

        self._handle(bot, "/invite")

        self.assertIn("mysecret", self.messages[0][1])
        self.assertIn("t.me/BlackframeBot", self.messages[0][1])

    def test_non_admin_invite_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            Path(guests_file).write_text(
                json.dumps({"456": {"name": "Paola", "joined_at": "2026-01-01T00:00:00"}})
            )
            bot, _ = self._bot(TELEGRAM_INVITE_CODE="mysecret", TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/invite", chat_id=456)

            self.assertIn("amministratori", self.messages[0][1])

    def test_guests_command_lists_guests(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            Path(guests_file).write_text(
                json.dumps({"456": {"name": "Paola", "joined_at": "2026-06-01T10:00:00"}})
            )
            bot, _ = self._bot(TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/guests")

            self.assertIn("Paola", self.messages[0][1])
            self.assertIn("456", self.messages[0][1])

    def test_revoke_removes_guest(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            Path(guests_file).write_text(
                json.dumps({"456": {"name": "Paola", "joined_at": "2026-01-01T00:00:00"}})
            )
            bot, _ = self._bot(TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/revoke 456")

            data = json.loads(Path(guests_file).read_text())
            self.assertNotIn("456", data)
            self.assertIn("rimosso", self.messages[0][1])

    def test_revoke_unknown_guest_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            guests_file = os.path.join(tmp, "guests.json")
            Path(guests_file).write_text(json.dumps({}))
            bot, _ = self._bot(TELEGRAM_GUESTS_FILE=guests_file)

            self._handle(bot, "/revoke 999")

            self.assertIn("non trovato", self.messages[0][1])


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

    def test_subsample_timed_packets_targets_record_fps(self):
        from blackframe.recording import subsample_timed_packets

        # 6 frames in 0.5s -> 6 buckets at 10 fps (one every 0.1s).
        packets = [(index * 0.1, b"jpeg", index + 1) for index in range(6)]
        sampled = subsample_timed_packets(packets, fps=10.0)
        self.assertEqual(len(sampled), 6)
        dense = [(index * 0.05, b"jpeg", index + 1) for index in range(20)]
        self.assertEqual(len(subsample_timed_packets(dense, fps=10.0)), 10)

    def test_write_frames_at_fps_skips_duplicate_sequences(self):
        import numpy as np

        from blackframe.recording import _write_frames_at_fps

        class SeqCamera:
            def __init__(self):
                self.seq = 0
                self.frame = np.zeros((48, 64, 3), dtype=np.uint8)

            def get_raw_frame_packet(self):
                self.seq += 1
                return self.frame.copy(), self.seq

        import cv2

        path = Path(tempfile.mkdtemp()) / "seq.mp4"
        writer = cv2.VideoWriter(
            str(path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            5.0,
            (64, 48),
        )
        if not writer.isOpened():
            self.skipTest("VideoWriter non disponibile")
        stop = __import__("threading").Event()
        camera = SeqCamera()
        _write_frames_at_fps(
            camera,
            writer,
            (64, 48),
            stop,
            fps=5.0,
            max_duration=0.5,
            started_at=__import__("time").time(),
        )
        writer.release()
        from blackframe.recording import _video_duration_sec

        duration = _video_duration_sec(path) or 0.0
        # 5 unique frames/sec for 0.5s -> ~2-3 frames, not 25 duplicates of the same frame.
        self.assertGreaterEqual(duration, 0.2)
        self.assertLessEqual(duration, 0.8)


def _write_mp4v_clip(path: Path, frames: int = 6, size=(64, 48), fps: float = 10.0) -> bool:
    """Write a tiny mp4v-coded clip with OpenCV; returns False if the writer fails."""
    import cv2
    import numpy as np

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not writer.isOpened():
        return False
    for index in range(frames):
        frame = np.full((size[1], size[0], 3), index * 10 % 255, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return path.is_file() and path.stat().st_size > 0


class OpenMp4WriterTests(unittest.TestCase):
    def test_windows_skips_avc1(self):
        from blackframe.recording import _writer_fourcc_tags

        with mock.patch("blackframe.recording.sys.platform", "win32"):
            self.assertEqual(_writer_fourcc_tags(), ("mp4v",))

    def test_non_windows_tries_avc1_first(self):
        from blackframe.recording import _writer_fourcc_tags

        with mock.patch("blackframe.recording.sys.platform", "linux"):
            self.assertEqual(_writer_fourcc_tags(), ("avc1", "mp4v"))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe richiesti")
class FinalizeRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="finalize-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_transcode_true_rewrites_mp4v_to_h264(self):
        from blackframe.recording import _video_codec, finalize_recording

        path = self.tmpdir / "clip.mp4"
        if not _write_mp4v_clip(path):
            self.skipTest("VideoWriter mp4v non disponibile in questo build OpenCV")
        self.assertEqual(_video_codec(path), "mpeg4")
        finalize_recording(path, transcode=True)
        self.assertEqual(_video_codec(path), "h264")

    def test_transcode_false_keeps_codec(self):
        from blackframe.recording import _video_codec, finalize_recording

        path = self.tmpdir / "segment.mp4"
        if not _write_mp4v_clip(path):
            self.skipTest("VideoWriter mp4v non disponibile in questo build OpenCV")
        finalize_recording(path, transcode=False)
        # Faststart only: still mpeg4, not transcoded.
        self.assertEqual(_video_codec(path), "mpeg4")


class NotificationDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="notify-dedup-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_notified_marker_persists_on_disk(self):
        store = make_store(self.tmpdir)
        event_id = "motion_event_20260417_120000"
        (self.tmpdir / event_id).mkdir(parents=True)

        self.assertFalse(store.event_was_notified(event_id))
        store.mark_event_notified(event_id)
        self.assertTrue(store.event_was_notified(event_id))
        # A fresh store (simulating a restart) still sees the marker.
        self.assertTrue(make_store(self.tmpdir).event_was_notified(event_id))


class BitrateEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="bitrate-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_uses_constant_when_no_samples(self):
        from blackframe.continuous_recording import MP4V_BITS_PER_PIXEL, estimate_bitrate_bps

        bitrate, calibrated = estimate_bitrate_bps(640, 360, 10, sample_dir=self.tmpdir)
        self.assertFalse(calibrated)
        self.assertAlmostEqual(bitrate, 640 * 360 * 10 * MP4V_BITS_PER_PIXEL)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe richiesti"
    )
    def test_calibrates_from_real_segments(self):
        from blackframe.continuous_recording import estimate_bitrate_bps

        segment = self.tmpdir / "segment_20260417_120000.mp4"
        if not _write_mp4v_clip(segment, frames=20):
            self.skipTest("VideoWriter mp4v non disponibile in questo build OpenCV")
        bitrate, calibrated = estimate_bitrate_bps(640, 360, 10, sample_dir=self.tmpdir)
        self.assertTrue(calibrated)
        self.assertGreater(bitrate, 0)


class WifiServiceTests(unittest.TestCase):
    def setUp(self):
        from blackframe.service_layer import WifiService

        self.wifi = WifiService()

    def test_macos_ssid_via_ipconfig_fallback(self):
        summary = "  LinkStatusActive : TRUE\n  SSID : CasaWifi\n  BSSID : aa:bb\n"
        with mock.patch.object(self.wifi, "_run_command", return_value=summary):
            self.assertEqual(self.wifi._macos_ssid_ipconfig("en0"), "CasaWifi")

    def test_macos_ipconfig_ignores_bssid_only(self):
        with mock.patch.object(self.wifi, "_run_command", return_value="  BSSID : aa:bb\n"):
            self.assertIsNone(self.wifi._macos_ssid_ipconfig("en0"))

    def test_windows_netsh_parses_ssid(self):
        output = (
            "    Name      : Wi-Fi\n"
            "    SSID      : Ufficio\n"
            "    BSSID     : aa:bb\n"
            "    State     : connected\n"
        )
        with mock.patch.object(self.wifi, "_run_command", return_value=output):
            info = self.wifi._detect_windows_wifi()
        self.assertEqual(info["ssid"], "Ufficio")
        self.assertTrue(info["connected"])

    def test_windows_no_connection_when_empty(self):
        with mock.patch.object(self.wifi, "_run_command", return_value=""):
            self.assertIsNone(self.wifi._detect_windows_wifi())


class MotionRouteIdTests(unittest.TestCase):
    def test_event_id_pattern_accepts_category_suffix(self):
        from blackframe.routes.motion import EVENT_ID_PATTERN

        self.assertTrue(EVENT_ID_PATTERN.fullmatch("motion_event_20260623_230013__persona"))
        self.assertTrue(EVENT_ID_PATTERN.fullmatch("motion_event_20260623_230013"))
        self.assertFalse(EVENT_ID_PATTERN.fullmatch("motion_event_20260623_230013__hacker"))


if __name__ == "__main__":
    unittest.main()
