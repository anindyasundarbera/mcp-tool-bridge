import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.claude import build_claude_command
from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy


def settings(config_path: Path) -> Settings:
    return Settings.from_env({"PROJECTS_CONFIG_PATH": str(config_path)})


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Test",
        "root": str(root),
        "capabilities": ["repository_read", "claude_edit"],
        "allow_ultracode": True,
    }
    data.update(overrides)
    return ProjectPolicy.model_validate(data)


class ClaudeCommandTests(unittest.TestCase):
    def test_selected_effort_is_passed_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(settings(root / "projects.json"), policy(root), "ultracode")

        self.assertEqual(command.count("--effort"), 1)
        self.assertEqual(command[command.index("--effort") + 1], "ultracode")

    def test_preserves_restricted_tool_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(settings(root / "projects.json"), policy(root), "high")

        self.assertIn("Read,Glob,Grep,Edit,Write", command)
        self.assertIn("Bash", command)
        self.assertIn("mcp__*", command)
        self.assertNotIn("bypassPermissions", command)

    def test_rejects_ultracode_when_project_disallows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, allow_ultracode=False)
            with self.assertRaisesRegex(ValueError, "does not allow ultracode"):
                build_claude_command(settings(root / "projects.json"), project, "ultracode")

    def test_project_overrides_model_and_turn_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, claude_model="custom:model", claude_max_turns=17)
            command = build_claude_command(settings(root / "projects.json"), project, "high")

        self.assertEqual(command[command.index("--model") + 1], "custom:model")
        self.assertEqual(command[command.index("--max-turns") + 1], "17")


if __name__ == "__main__":
    unittest.main()

