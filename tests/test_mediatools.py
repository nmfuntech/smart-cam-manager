import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from blackframe import mediatools


class FindToolTests(unittest.TestCase):
    def setUp(self):
        mediatools.find_tool.cache_clear()
        self.addCleanup(mediatools.find_tool.cache_clear)

    def test_env_override_wins(self):
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "ffmpeg.exe"
            exe.write_bytes(b"")
            with mock.patch.dict(os.environ, {"FFMPEG_BIN": f'"{exe}"'}):
                self.assertEqual(mediatools.ffmpeg_bin(), str(exe))

    def test_falls_back_to_path(self):
        with mock.patch.dict(os.environ, {"FFPROBE_BIN": ""}), mock.patch.object(
            mediatools.shutil, "which", return_value="/usr/bin/ffprobe"
        ):
            self.assertEqual(mediatools.ffprobe_bin(), "/usr/bin/ffprobe")

    def test_windows_service_finds_user_winget_install(self):
        with TemporaryDirectory() as tmp:
            exe = Path(tmp) / "ffmpeg.exe"
            exe.write_bytes(b"")
            with mock.patch.dict(os.environ, {"FFMPEG_BIN": ""}), mock.patch.object(
                mediatools.shutil, "which", return_value=None
            ), mock.patch.object(mediatools, "_IS_WINDOWS", True), mock.patch.object(
                mediatools, "_windows_candidates", return_value=[str(exe)]
            ):
                self.assertEqual(mediatools.ffmpeg_bin(), str(exe))

    def test_windows_candidates_cover_winget_packages_of_any_user(self):
        with mock.patch.object(mediatools.glob, "glob", return_value=[]) as fake_glob:
            mediatools._windows_candidates("ffmpeg")
        patterns = [call.args[0] for call in fake_glob.call_args_list]
        self.assertTrue(any("WinGet" in p and "*FFmpeg*" in p for p in patterns))
        self.assertTrue(any("chocolatey" in p for p in patterns))

    def test_missing_everywhere_returns_none(self):
        with mock.patch.dict(os.environ, {"FFMPEG_BIN": ""}), mock.patch.object(
            mediatools.shutil, "which", return_value=None
        ), mock.patch.object(mediatools, "_IS_WINDOWS", False):
            self.assertIsNone(mediatools.ffmpeg_bin())


if __name__ == "__main__":
    unittest.main()
