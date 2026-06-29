import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import env_profiles, windows_wizard


class EnvProfilesTests(unittest.TestCase):
    def test_patch_env_file_updates_existing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "MOTION_THRESHOLD=48\nTAPO_HOST=192.168.1.1\n",
                encoding="utf-8",
            )
            updated = env_profiles.patch_env_file(path, {"MOTION_THRESHOLD": "55"})
            text = path.read_text(encoding="utf-8")
            self.assertEqual(updated, ["MOTION_THRESHOLD"])
            self.assertIn("MOTION_THRESHOLD=55", text)
            self.assertIn("TAPO_HOST=192.168.1.1", text)

    def test_patch_env_file_appends_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TAPO_HOST=10.0.0.1\n", encoding="utf-8")
            env_profiles.patch_env_file(path, {"MOTION_SCALE_WIDTH": "360"})
            self.assertIn("MOTION_SCALE_WIDTH=360", path.read_text(encoding="utf-8"))

    def test_apply_profile_unknown_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TAPO_HOST=1.1.1.1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                env_profiles.apply_profile(path, "nonexistent")

    def test_mini_pc_profile_contains_stream2(self):
        self.assertEqual(env_profiles.MINI_PC_WINDOWS["TAPO_STREAM_PATH"], "stream2")

    def test_mini_pc_profile_matches_validated_optimal_values(self):
        # Guardia contro divergenze: questi valori sono stati validati sul campo per
        # rilevamento persona affidabile + accensione automazione a bassa latenza.
        expected = {
            "MOTION_THRESHOLD": "30",
            "MOTION_MIN_AREA": "2500",
            "MOTION_FRAME_INTERVAL": "0.25",
            "MOTION_TRIGGER_FRAMES": "4",
            "MOTION_WARMUP_FRAMES": "35",
            "MOTION_GLOBAL_CHANGE_RATIO": "0.5",
            "MOTION_MORPH_DILATE_ITER": "1",
            "RTSP_BACKLOG_SKIP_FRAMES": "1",
            "CLASSIFICATION_ENABLED": "true",
            "CLASSIFICATION_BACKEND": "detection",
            "CLASSIFICATION_MIN_CONFIDENCE": "0.5",
            "CLASSIFICATION_CROP_TO_MOTION": "true",
            "CLASSIFICATION_SAMPLE_POLICY": "event_cover",
        }
        for key, value in expected.items():
            self.assertEqual(
                env_profiles.MINI_PC_WINDOWS.get(key),
                value,
                msg=f"Profilo MINI_PC_WINDOWS divergente su {key}",
            )

    def test_apply_profile_writes_optimal_classification_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TAPO_HOST=1.1.1.1\n", encoding="utf-8")
            env_profiles.apply_profile(path, "mini-pc-windows")
            text = path.read_text(encoding="utf-8")
            self.assertIn("CLASSIFICATION_CROP_TO_MOTION=true", text)
            self.assertIn("CLASSIFICATION_SAMPLE_POLICY=event_cover", text)
            self.assertIn("CLASSIFICATION_MIN_CONFIDENCE=0.5", text)


class WindowsWizardTests(unittest.TestCase):
    def test_parse_args_accepts_argv(self):
        # Regressione: parse_args veniva chiamata con argv ma definita senza
        # parametri, facendo crashare il wizard appena avviato.
        args = windows_wizard.parse_args(["--root", ".", "--non-interactive"])
        self.assertTrue(args.non_interactive)
        self.assertEqual(args.root, ".")

    def test_non_interactive_applies_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("TAPO_HOST=1.1.1.1\n", encoding="utf-8")
            env_path = root / ".env"
            env_path.write_text("TAPO_HOST=1.1.1.1\n", encoding="utf-8")
            rc = windows_wizard.main(
                ["--root", str(root), "--env-file", ".env", "--non-interactive"]
            )
            self.assertEqual(rc, 0)
            text = env_path.read_text(encoding="utf-8")
            self.assertIn("CLASSIFICATION_SAMPLE_POLICY=event_cover", text)
            self.assertIn("TAPO_STREAM_PATH=stream2", text)


class CheckPrerequisitesTests(unittest.TestCase):
    def test_check_ffmpeg_missing(self):
        mod = __import__("scripts.check_prerequisites", fromlist=["check_ffmpeg"])
        with mock.patch("shutil.which", return_value=None):
            issues = mod.check_ffmpeg()
        self.assertTrue(any("ffmpeg" in item for item in issues))

    def test_check_classification_models_when_enabled_and_missing(self):
        mod = __import__("scripts.check_prerequisites", fromlist=["check_classification_models"])
        with mock.patch.dict(
            os.environ,
            {
                "CLASSIFICATION_ENABLED": "true",
                "CLASSIFICATION_BACKEND": "detection",
                "CLASSIFICATION_DETECTION_MODEL_PATH": "models/missing.pb",
                "CLASSIFICATION_DETECTION_CONFIG_PATH": "models/missing.pbtxt",
            },
            clear=False,
        ):
            with mock.patch.object(mod, "ROOT", Path(__file__).resolve().parent.parent):
                issues = mod.check_classification_models()
        self.assertEqual(len(issues), 1)
        self.assertIn("fetch-model", issues[0])

    def test_check_motion_tuning_warns_high_min_area(self):
        mod = __import__("scripts.check_prerequisites", fromlist=["check_motion_tuning"])
        with mock.patch.dict(os.environ, {"MOTION_MIN_AREA": "5000"}, clear=False):
            issues = mod.check_motion_tuning()
        self.assertTrue(any("MOTION_MIN_AREA" in item for item in issues))


if __name__ == "__main__":
    unittest.main()
