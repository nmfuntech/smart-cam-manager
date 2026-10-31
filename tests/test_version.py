import tempfile
import unittest
from pathlib import Path

from blackframe.version import _read_git_head

SHA = "d2b2c822e1809962cbd6266acfe0d92248588c10"


class ReadGitHeadTests(unittest.TestCase):
    def _repo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".git" / "refs" / "heads").mkdir(parents=True)
        return root

    def test_loose_ref(self):
        root = self._repo()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / ".git" / "refs" / "heads" / "main").write_text(SHA + "\n")
        self.assertEqual(_read_git_head(root), SHA)

    def test_packed_ref(self):
        root = self._repo()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (root / ".git" / "packed-refs").write_text(f"# pack-refs\n{SHA} refs/heads/main\n")
        self.assertEqual(_read_git_head(root), SHA)

    def test_detached_head(self):
        root = self._repo()
        (root / ".git" / "HEAD").write_text(SHA + "\n")
        self.assertEqual(_read_git_head(root), SHA)

    def test_missing_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_read_git_head(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
