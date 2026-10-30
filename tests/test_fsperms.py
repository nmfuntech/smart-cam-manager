import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from blackframe import fsperms


class RestrictWindowsAclTests(unittest.TestCase):
    def test_removes_broad_groups_by_sid_after_disabling_inheritance(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return mock.Mock(returncode=0, stderr=b"")

        with mock.patch.object(fsperms.subprocess, "run", side_effect=fake_run):
            self.assertTrue(fsperms.restrict_windows_acl(Path("data")))

        self.assertEqual(calls[0][:3], ["icacls", "data", "/inheritance:d"])
        self.assertEqual(calls[1][:3], ["icacls", "data", "/remove:g"])
        for sid in ("*S-1-1-0", "*S-1-5-11", "*S-1-5-32-545"):
            self.assertIn(sid, calls[1])
        # Owner/Administrators/SYSTEM are never touched.
        self.assertNotIn("*S-1-5-18", calls[1])
        self.assertNotIn("*S-1-5-32-544", calls[1])

    def test_failure_is_reported_not_raised(self):
        with mock.patch.object(
            fsperms.subprocess, "run", return_value=mock.Mock(returncode=5, stderr=b"denied")
        ):
            self.assertFalse(fsperms.restrict_windows_acl(Path("data")))
        with mock.patch.object(fsperms.subprocess, "run", side_effect=OSError("no icacls")):
            self.assertFalse(fsperms.restrict_windows_acl(Path("data")))


class HardenWindowsPathsTests(unittest.TestCase):
    def test_noop_outside_windows(self):
        with mock.patch.object(fsperms, "IS_WINDOWS", False), mock.patch.object(
            fsperms, "restrict_windows_acl"
        ) as restrict:
            fsperms.harden_windows_paths([Path(".")])
        restrict.assert_not_called()

    def test_directory_hardened_once_via_marker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(fsperms, "IS_WINDOWS", True), mock.patch.object(
                fsperms, "restrict_windows_acl", return_value=True
            ) as restrict:
                fsperms.harden_windows_paths([root])
                fsperms.harden_windows_paths([root])
            self.assertEqual(restrict.call_count, 1)
            self.assertTrue((root / fsperms.WINDOWS_MARKER_NAME).exists())

    def test_missing_paths_are_skipped(self):
        with mock.patch.object(fsperms, "IS_WINDOWS", True), mock.patch.object(
            fsperms, "restrict_windows_acl"
        ) as restrict:
            fsperms.harden_windows_paths([Path("/definitely/not/here")])
        restrict.assert_not_called()


if __name__ == "__main__":
    unittest.main()
