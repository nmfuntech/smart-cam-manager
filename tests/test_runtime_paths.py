import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import runtime_paths


class RuntimePathsTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only")
    def test_installed_data_home_with_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "BLACKFRAME"
            home.mkdir()
            (home / runtime_paths.INSTALLED_MARKER).write_text("ok", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PROGRAMDATA": tmp}, clear=False):
                self.assertEqual(runtime_paths.installed_data_home(), home)

    @unittest.skipUnless(os.name == "nt", "Windows-only")
    def test_installed_data_home_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PROGRAMDATA": tmp}, clear=False):
                self.assertIsNone(runtime_paths.installed_data_home())

    def test_installed_data_home_with_explicit_env(self):
        with mock.patch.dict(os.environ, {"BLACKFRAME_HOME": "D:\\bf-data"}, clear=False):
            self.assertEqual(runtime_paths.installed_data_home(), Path("D:\\bf-data"))

    def test_runtime_python_prefers_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled = root / "runtime" / "python"
            bundled.mkdir(parents=True)
            exe = bundled / "python.exe"
            exe.write_text("", encoding="utf-8")
            self.assertEqual(runtime_paths.runtime_python(root), exe)

    def test_configure_runtime_uses_cwd_when_not_installed(self):
        with mock.patch.object(runtime_paths, "installed_data_home", return_value=None):
            with mock.patch("dotenv.load_dotenv") as load_dotenv:
                cwd = runtime_paths.configure_runtime_environment()
                load_dotenv.assert_called_once()
                self.assertEqual(cwd, Path.cwd())


if __name__ == "__main__":
    unittest.main()
