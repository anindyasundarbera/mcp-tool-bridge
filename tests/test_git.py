import asyncio
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.git import project_status

from tests.support import initialize_git_repository


class GitObservationTests(unittest.TestCase):
    def test_clean_and_dirty_status_include_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git_repository(root)
            clean = asyncio.run(project_status(root))
            self.assertTrue(clean["clean"])

            (root / "new.md").write_text("new\n", encoding="utf-8")
            dirty = asyncio.run(project_status(root))
            self.assertFalse(dirty["clean"])
            self.assertIn("new.md", dirty["working_tree"])


if __name__ == "__main__":
    unittest.main()

