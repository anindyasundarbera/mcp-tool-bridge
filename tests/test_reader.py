import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.reader import RepositoryReadError, RepositoryReader

from tests.support import initialize_git_repository


def settings(root: Path, **overrides: str) -> Settings:
    environment = {"PROJECTS_CONFIG_PATH": str(root / "projects.json")}
    environment.update(overrides)
    return Settings.from_env(environment)


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Reader tests",
        "root": str(root),
        "capabilities": ["repository_read"],
        "protected_paths": [".git", ".env", "private"],
    }
    data.update(overrides)
    return ProjectPolicy.model_validate(data)


class RepositoryReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        initialize_git_repository(self.root)
        self.reader = RepositoryReader(settings(self.root))
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reads_markdown_with_hash_and_git_metadata(self) -> None:
        content = "# Title\n\nBody\n"
        path = self.root / "docs" / "guide.md"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")

        result = asyncio.run(self.reader.read_markdown(self.project, "docs/guide.md"))

        self.assertEqual(result["content"], content)
        self.assertEqual(result["line_count"], 3)
        self.assertEqual(
            result["sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        self.assertIn("?? docs/guide.md", result["git_status"])
        self.assertNotIn(str(self.root), str(result))

    def test_lists_only_bounded_visible_unprotected_markdown(self) -> None:
        (self.root / "docs" / "nested").mkdir(parents=True)
        (self.root / "docs" / "one.md").write_text("one", encoding="utf-8")
        (self.root / "docs" / "two.txt").write_text("two", encoding="utf-8")
        (self.root / "docs" / "nested" / "three.md").write_text("three", encoding="utf-8")
        (self.root / ".hidden.md").write_text("hidden", encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private" / "secret.md").write_text("secret", encoding="utf-8")

        shallow = asyncio.run(
            self.reader.list_markdown(self.project, "docs", max_depth=0)
        )
        deep = asyncio.run(self.reader.list_markdown(self.project, ".", max_depth=4))

        self.assertEqual([item["path"] for item in shallow["files"]], ["docs/one.md"])
        self.assertEqual(
            [item["path"] for item in deep["files"]],
            ["README.md", "docs/one.md", "docs/nested/three.md"],
        )

    def test_rejects_traversal_hidden_protected_non_markdown_and_symlinks(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "file.txt").write_text("text", encoding="utf-8")
        (self.root / "private").mkdir()
        (self.root / "private" / "secret.md").write_text("secret", encoding="utf-8")
        (self.root / "alias").symlink_to(self.root / "private", target_is_directory=True)

        for requested in (
            "../outside.md",
            ".git/config.md",
            "private/secret.md",
            "alias/secret.md",
            "docs/file.txt",
        ):
            with self.subTest(requested=requested):
                with self.assertRaises(RepositoryReadError):
                    asyncio.run(self.reader.read_markdown(self.project, requested))

    def test_reads_ranges_with_optimistic_hash_guard(self) -> None:
        content = "one\ntwo\nthree\nfour\n"
        (self.root / "lines.md").write_text(content, encoding="utf-8")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        result = asyncio.run(
            self.reader.read_range(self.project, "lines.md", 2, 3, digest)
        )
        self.assertEqual(result["content"], "two\nthree\n")
        self.assertEqual(result["total_lines"], 4)

        with self.assertRaisesRegex(RepositoryReadError, "does not match"):
            asyncio.run(
                self.reader.read_range(self.project, "lines.md", 1, 1, "0" * 64)
            )

    def test_outline_supports_atx_and_setext_and_skips_fences(self) -> None:
        content = (
            "# Main\n\n"
            "Section\n-------\n\n"
            "```md\n# Not a heading\n```\n\n"
            "### Child ###\n"
        )
        (self.root / "outline.md").write_text(content, encoding="utf-8")

        result = asyncio.run(self.reader.outline(self.project, "outline.md"))

        self.assertEqual(
            result["headings"],
            [
                {"level": 1, "text": "Main", "line": 1},
                {"level": 2, "text": "Section", "line": 3},
                {"level": 3, "text": "Child", "line": 10},
            ],
        )

    def test_search_is_literal_case_insensitive_and_excludes_protected_files(self) -> None:
        (self.root / "docs").mkdir()
        (self.root / "docs" / "one.md").write_text(
            "A [literal] value\nsecond literal value\n", encoding="utf-8"
        )
        (self.root / "private").mkdir()
        (self.root / "private" / "secret.md").write_text(
            "[literal] secret\n", encoding="utf-8"
        )

        result = asyncio.run(
            self.reader.search(self.project, "[LITERAL]", max_results=10)
        )

        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["path"], "docs/one.md")
        self.assertEqual(result["matches"][0]["line"], 1)

    def test_file_size_and_range_limits_are_enforced(self) -> None:
        constrained = RepositoryReader(
            settings(
                self.root,
                MAX_MARKDOWN_FILE_BYTES="8",
                MAX_MARKDOWN_RANGE_LINES="2",
            )
        )
        (self.root / "large.md").write_text("123456789", encoding="utf-8")
        with self.assertRaisesRegex(RepositoryReadError, "byte limit"):
            asyncio.run(constrained.read_markdown(self.project, "large.md"))

        (self.root / "small.md").write_text("1\n2\n3\n", encoding="utf-8")
        with self.assertRaisesRegex(RepositoryReadError, "line range"):
            asyncio.run(constrained.read_range(self.project, "small.md", 1, 3))


if __name__ == "__main__":
    unittest.main()
