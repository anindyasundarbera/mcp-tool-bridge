import unittest
from pathlib import Path

from local_dev_mcp.config import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_and_relative_registry_resolution(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual(settings.claude_model, "glm-5.2:cloud")
        self.assertEqual(settings.mcp_port, 8000)
        self.assertEqual(settings.max_markdown_file_bytes, 1048576)
        self.assertEqual(settings.max_markdown_range_lines, 1000)
        self.assertEqual(settings.max_markdown_write_bytes, 1048576)
        self.assertEqual(settings.max_markdown_diff_bytes, 262144)
        self.assertTrue(settings.projects_config_path.is_absolute())
        self.assertEqual(settings.projects_config_path.name, "projects.json")

    def test_rejects_non_positive_limits(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"CLAUDE_MAX_TURNS": "0"})
        with self.assertRaisesRegex(RuntimeError, "MAX_MARKDOWN_WRITE_BYTES"):
            Settings.from_env({"MAX_MARKDOWN_WRITE_BYTES": "not-an-integer"})
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"MAX_MARKDOWN_DIFF_BYTES": "0"})

    def test_accepts_absolute_config_path(self) -> None:
        settings = Settings.from_env({"PROJECTS_CONFIG_PATH": "/tmp/projects.json"})
        self.assertEqual(settings.projects_config_path, Path("/tmp/projects.json"))


if __name__ == "__main__":
    unittest.main()
