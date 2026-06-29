import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import env_profiles, windows_service


class WindowsEnvExampleTests(unittest.TestCase):
    def test_build_example_applies_mini_pc_values(self):
        base = Path(__file__).resolve().parent.parent / ".env.example"
        content = env_profiles.build_example_content(base, env_profiles.MINI_PC_WINDOWS)
        self.assertIn("TAPO_STREAM_PATH=stream2", content)
        self.assertIn("MOTION_SCALE_WIDTH=420", content)
        self.assertIn("RECORD_ENABLED=true", content)

    def test_write_windows_minipc_example_creates_file(self):
        with mock.patch.object(
            env_profiles,
            "build_example_content",
            return_value="TAPO_HOST=1.1.1.1\nTAPO_STREAM_PATH=stream2\n",
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / ".env.windows-minipc.example"
                base = Path(tmp) / ".env.example"
                base.write_text("TAPO_HOST=1.1.1.1\n", encoding="utf-8")
                env_profiles.write_windows_minipc_example(base_path=base, output_path=out)
                text = out.read_text(encoding="utf-8")
                self.assertIn("mini PC Windows", text)
                self.assertIn("TAPO_STREAM_PATH=stream2", text)


class WindowsServiceTests(unittest.TestCase):
    def test_sc_query_missing_service(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1060, stdout="", stderr="")
            status = windows_service.sc_query("MISSING")
        self.assertEqual(status["exists"], "false")

    def test_sc_query_running_service(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(
                returncode=0,
                stdout="STATE              : 4  RUNNING\n",
                stderr="",
            )
            status = windows_service.sc_query("BLACKFRAME")
        self.assertEqual(status["exists"], "true")
        self.assertEqual(status["state"], "running")

    def test_list_port_listeners_parses_netstat(self):
        sample = "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       1234\n"
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=sample, stderr="")
            listeners = windows_service.list_port_listeners(8000)
        self.assertEqual(len(listeners), 1)
        self.assertEqual(listeners[0]["pid"], 1234)

    def test_health_check_ok(self):
        payload = json.dumps({"status": "ok"}).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertTrue(windows_service.health_check(port=8000))

    def test_find_nssm_from_path(self):
        with mock.patch("shutil.which", return_value=r"C:\Tools\nssm\nssm.exe"):
            found = windows_service.find_nssm()
        self.assertEqual(str(found), r"C:\Tools\nssm\nssm.exe")


if __name__ == "__main__":
    unittest.main()
