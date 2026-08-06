import asyncio
import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.writer import RepositoryWriteError, RepositoryWriter

from tests.support import initialize_git_repository


def settings(root: Path, **overrides: str) -> Settings:
    environment = {"PROJECTS_CONFIG_PATH": str(root / "projects.json")}
    environment.update(overrides)
    return Settings.from_env(environment)


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Writer tests",
        "root": str(root),
        "capabilities": ["repository_read", "markdown_write", "claude_edit"],
        "markdown_write_roots": ["docs", "canonical"],
        "protected_paths": [".git", ".env", "private", "docs/protected"],
    }
    data.update(overrides)
    return ProjectPolicy.model_validate(data)


class RepositoryWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        initialize_git_repository(self.root)
        (self.root / "docs").mkdir()
        (self.root / "canonical").mkdir()
        self.writer = RepositoryWriter(settings(self.root))
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(
        self,
        path: str,
        content: str,
        write_mode: str = "create",
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        return asyncio.run(
            self.writer.write(
                self.project,
                path,
                content,
                write_mode,  # type: ignore[arg-type]
                expected_sha256,
            )
        )

    def test_create_preserves_exact_content_and_returns_evidence(self) -> None:
        content = "# Title\r\n\r\nBody without final newline"
        result = self.write("docs/new.md", content)

        target = self.root / "docs" / "new.md"
        self.assertEqual(target.read_bytes(), content.encode("utf-8"))
        self.assertEqual(result["project_id"], "test-project")
        self.assertEqual(result["path"], "docs/new.md")
        self.assertEqual(result["write_mode"], "create")
        self.assertIsNone(result["before_sha256"])
        self.assertEqual(
            result["after_sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        self.assertEqual(result["size_bytes"], len(content.encode("utf-8")))
        self.assertEqual(result["line_count"], 3)
        self.assertIn("?? docs/new.md", result["git_status"])
        self.assertTrue(str(result["diff"]).startswith("--- /dev/null\n+++ b/docs/new.md\n"))
        self.assertFalse(result["diff_truncated"])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode) & 0o111, 0)
        self.assertNotIn(str(self.root), str(result))

    def test_create_refuses_existing_target_without_changing_it(self) -> None:
        target = self.root / "docs" / "existing.md"
        target.write_bytes(b"before")

        with self.assertRaisesRegex(RepositoryWriteError, "already exists"):
            self.write("docs/existing.md", "after")

        self.assertEqual(target.read_bytes(), b"before")

    def test_update_requires_matching_hash_and_preserves_mode(self) -> None:
        target = self.root / "docs" / "existing.md"
        before = b"# Before\n"
        after = "# After\n\nBody\n"
        target.write_bytes(before)
        target.chmod(0o640)
        subprocess.run(["git", "-C", str(self.root), "add", "docs/existing.md"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add existing"], check=True
        )
        digest = hashlib.sha256(before).hexdigest()

        result = self.write("docs/existing.md", after, "update", digest.upper())

        self.assertEqual(target.read_bytes(), after.encode("utf-8"))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(result["before_sha256"], digest)
        self.assertEqual(
            result["after_sha256"], hashlib.sha256(after.encode("utf-8")).hexdigest()
        )
        self.assertIn("M docs/existing.md", result["git_status"])
        self.assertTrue(
            str(result["diff"]).startswith(
                "--- a/docs/existing.md\n+++ b/docs/existing.md\n"
            )
        )

    def test_hash_and_mode_rejections_leave_target_unchanged(self) -> None:
        target = self.root / "docs" / "guarded.md"
        target.write_bytes(b"original")
        attempts = (
            ("update", None, "required"),
            ("update", "bad", "64-character"),
            ("update", "0" * 64, "does not match"),
            ("create", "0" * 64, "forbidden"),
            ("replace", None, "write_mode"),
        )

        for mode, digest, message in attempts:
            with self.subTest(mode=mode, digest=digest):
                with self.assertRaisesRegex(RepositoryWriteError, message):
                    self.write("docs/guarded.md", "changed", mode, digest)
                self.assertEqual(target.read_bytes(), b"original")

    def test_rejects_paths_outside_policy_without_mutation(self) -> None:
        for requested in (
            "/absolute.md",
            "../outside.md",
            "docs/../outside.md",
            "docs//empty-segment.md",
            "docs/./dot-segment.md",
            "docs/trailing.md/",
            ".hidden.md",
            "docs/.hidden.md",
            "docs/protected/secret.md",
            "README.md",
            "docs/not-markdown.txt",
        ):
            with self.subTest(requested=requested):
                with self.assertRaises(RepositoryWriteError):
                    self.write(requested, "content")
        self.assertEqual(list((self.root / "docs").iterdir()), [])

    def test_rejects_symlink_components_and_targets(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.root / "docs" / "linked").symlink_to(outside, target_is_directory=True)
        (outside / "target.md").write_text("outside", encoding="utf-8")
        (self.root / "docs" / "alias.md").symlink_to(outside / "target.md")

        for requested in ("docs/linked/new.md", "docs/alias.md"):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(RepositoryWriteError, "symbolic"):
                    self.write(requested, "changed", "update", "0" * 64)
        self.assertEqual((outside / "target.md").read_text(encoding="utf-8"), "outside")

    def test_rejects_missing_parent_missing_target_and_non_regular_target(self) -> None:
        with self.assertRaisesRegex(RepositoryWriteError, "parent directory"):
            self.write("docs/missing/new.md", "content")
        with self.assertRaisesRegex(RepositoryWriteError, "does not exist"):
            self.write("docs/missing.md", "content", "update", "0" * 64)

        directory_target = self.root / "docs" / "directory.md"
        directory_target.mkdir()
        with self.assertRaisesRegex(RepositoryWriteError, "regular file"):
            self.write("docs/directory.md", "content", "update", "0" * 64)

        fifo_target = self.root / "docs" / "pipe.md"
        os.mkfifo(fifo_target)
        with self.assertRaisesRegex(RepositoryWriteError, "regular file"):
            self.write("docs/pipe.md", "content", "update", "0" * 64)

    def test_content_limit_and_invalid_unicode_are_rejected_before_creation(self) -> None:
        constrained = RepositoryWriter(
            settings(self.root, MAX_MARKDOWN_WRITE_BYTES="5")
        )
        with self.assertRaisesRegex(RepositoryWriteError, "write limit"):
            asyncio.run(
                constrained.write(self.project, "docs/large.md", "123456")
            )
        with self.assertRaisesRegex(RepositoryWriteError, "valid UTF-8"):
            self.write("docs/invalid.md", "\ud800")
        self.assertFalse((self.root / "docs" / "large.md").exists())
        self.assertFalse((self.root / "docs" / "invalid.md").exists())

    def test_diff_is_bounded_and_reports_truncation(self) -> None:
        constrained = RepositoryWriter(
            settings(self.root, MAX_MARKDOWN_DIFF_BYTES="40")
        )
        result = asyncio.run(
            constrained.write(self.project, "docs/large-diff.md", "line\n" * 100)
        )

        self.assertTrue(result["diff_truncated"])
        self.assertLessEqual(len(str(result["diff"]).encode("utf-8")), 40)

    def test_update_cleans_temporary_file_when_replace_fails(self) -> None:
        target = self.root / "docs" / "failure.md"
        target.write_bytes(b"before")
        digest = hashlib.sha256(b"before").hexdigest()

        with patch("local_dev_mcp.writer.os.replace", side_effect=OSError("boom")):
            with self.assertRaisesRegex(RepositoryWriteError, "replace"):
                self.write("docs/failure.md", "after", "update", digest)

        self.assertEqual(target.read_bytes(), b"before")
        self.assertEqual(list((self.root / "docs").glob(".failure.md.*.tmp")), [])

    def test_update_rechecks_current_bytes_before_replace(self) -> None:
        target = self.root / "docs" / "race.md"
        target.write_bytes(b"before")
        digest = hashlib.sha256(b"before").hexdigest()

        from local_dev_mcp import writer as writer_module

        original_read = writer_module._read_regular_file
        calls = 0

        def changed_on_recheck(directory_fd: int, name: str, limit: int) -> tuple[bytes, int]:
            nonlocal calls
            calls += 1
            data, mode = original_read(directory_fd, name, limit)
            return (b"external change", mode) if calls == 2 else (data, mode)

        with patch(
            "local_dev_mcp.writer._read_regular_file",
            side_effect=changed_on_recheck,
        ):
            with self.assertRaisesRegex(RepositoryWriteError, "changed during"):
                self.write("docs/race.md", "after", "update", digest)

        self.assertEqual(target.read_bytes(), b"before")
        self.assertEqual(list((self.root / "docs").glob(".race.md.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
