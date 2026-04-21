import base64
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import setup_config


class SetupConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="setup-config-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_generate_profile_encryption_key_is_valid_fernet_length(self):
        key = setup_config.generate_profile_encryption_key()
        decoded = base64.urlsafe_b64decode(key.encode("ascii"))

        self.assertEqual(len(decoded), 32)

    def test_secure_cookie_default_depends_on_bind_host(self):
        self.assertEqual(
            setup_config._secure_cookie_default({"APP_BIND_HOST": "127.0.0.1"}),
            "false",
        )
        self.assertEqual(
            setup_config._secure_cookie_default({"APP_BIND_HOST": "0.0.0.0"}),
            "true",
        )

    def test_build_env_content_quotes_values_with_spaces(self):
        content = setup_config.build_env_content(
            {
                "APP_ADMIN_USERNAME": "admin",
                "TAPO_CAMERA_NAME": "Camera salotto",
            }
        )

        self.assertIn('TAPO_CAMERA_NAME="Camera salotto"', content)

    def test_write_env_file_sets_private_permissions(self):
        env_path = Path(self.tmpdir) / ".env"

        setup_config.write_env_file(env_path, "APP_ADMIN_USERNAME=admin\n")

        self.assertTrue(env_path.exists())
        self.assertEqual(env_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env_path.read_text(encoding="utf-8"), "APP_ADMIN_USERNAME=admin\n")

    def test_profile_encryption_key_default_uses_existing_keyfile(self):
        key_path = Path(self.tmpdir) / "data" / ".camera_profiles.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("existing-key\n", encoding="utf-8")
        original_cwd = Path.cwd()
        os.chdir(self.tmpdir)
        try:
            resolved = setup_config._profile_encryption_key_default({})
        finally:
            os.chdir(original_cwd)

        self.assertEqual(resolved, "existing-key")

    def test_generated_value_message_contains_key_and_value(self):
        message = setup_config.generated_value_message("APP_SECRET_KEY", "abc123")

        self.assertEqual(message, "  Generato APP_SECRET_KEY: abc123")

    def test_selected_value_message_contains_key_and_value(self):
        message = setup_config.selected_value_message("TAPO_HOST", "192.168.1.120")

        self.assertEqual(message, "  Usato TAPO_HOST: 192.168.1.120")

    def test_selected_sections_minimal_only_contains_required_fields(self):
        sections = setup_config.selected_sections(minimal=True)
        keys = {
            field.key
            for section in sections
            for field in section.fields
        }

        self.assertEqual(
            keys,
            {
                "APP_ADMIN_PASSWORD",
                "APP_SECRET_KEY",
                "APP_PROFILE_ENCRYPTION_KEY",
                "TAPO_HOST",
                "TAPO_USERNAME",
                "TAPO_PASSWORD",
            },
        )

    def test_parse_args_accepts_minimal_flag(self):
        with mock.patch("sys.argv", ["setup_config.py", "--minimal"]):
            args = setup_config.parse_args()

        self.assertTrue(args.minimal)

    def test_cleanup_setup_state_removes_generated_local_state(self):
        env_path = Path(self.tmpdir) / ".env"
        data_dir = Path(self.tmpdir) / "data"
        captures_dir = Path(self.tmpdir) / "captures" / "motion" / "camera-a"
        data_dir.mkdir(parents=True, exist_ok=True)
        captures_dir.mkdir(parents=True, exist_ok=True)
        env_path.write_text("APP_ADMIN_USERNAME=admin\n", encoding="utf-8")
        (data_dir / ".camera_profiles.key").write_text("secret\n", encoding="utf-8")
        (data_dir / ".test-camera-profiles.key").write_text("secret\n", encoding="utf-8")
        (data_dir / "camera_profiles.json").write_text("{}", encoding="utf-8")
        (data_dir / "camera_profiles.json.unreadable.20260418_101800.bak").write_text(
            "{}",
            encoding="utf-8",
        )
        (captures_dir / "frame.jpg").write_text("x", encoding="utf-8")

        original_cwd = Path.cwd()
        os.chdir(self.tmpdir)
        try:
            removed = setup_config.cleanup_setup_state(env_path)
        finally:
            os.chdir(original_cwd)

        self.assertIn(env_path, removed)
        self.assertFalse(env_path.exists())
        self.assertFalse((data_dir / ".camera_profiles.key").exists())
        self.assertFalse((data_dir / ".test-camera-profiles.key").exists())
        self.assertFalse((data_dir / "camera_profiles.json").exists())
        self.assertFalse((data_dir / "camera_profiles.json.unreadable.20260418_101800.bak").exists())
        self.assertEqual(list((Path(self.tmpdir) / "captures" / "motion").rglob("*")), [])

    def test_prompt_secret_prints_used_existing_value_on_enter(self):
        field = setup_config.EnvField(
            key="APP_ADMIN_PASSWORD",
            prompt="Password admin",
            parser=setup_config.parse_text,
            secret=True,
            generator=setup_config.generate_admin_password,
        )
        stdout = io.StringIO()
        with mock.patch("scripts.setup_config.getpass", return_value=""):
            with mock.patch("sys.stdout", stdout):
                value, generated = setup_config.prompt_secret(field, "existing-secret")

        self.assertEqual(value, "existing-secret")
        self.assertFalse(generated)
        self.assertIn("Usato APP_ADMIN_PASSWORD: existing-secret", stdout.getvalue())

    def test_prompt_secret_generates_when_default_is_empty(self):
        field = setup_config.EnvField(
            key="APP_SECRET_KEY",
            prompt="APP_SECRET_KEY",
            parser=setup_config.parse_text,
            secret=True,
            generator=lambda: "generated-secret",
        )
        stdout = io.StringIO()
        with mock.patch("scripts.setup_config.getpass", return_value=""):
            with mock.patch("sys.stdout", stdout):
                value, generated = setup_config.prompt_secret(field, "")

        self.assertEqual(value, "generated-secret")
        self.assertTrue(generated)
        self.assertIn("Generato APP_SECRET_KEY: generated-secret", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
