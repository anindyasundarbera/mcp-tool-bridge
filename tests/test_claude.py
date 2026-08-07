import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.claude import build_claude_command, build_reviewer_command
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

    def test_resume_uses_stored_session_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json"),
                policy(root),
                "high",
                "bridge-session",
            )

        self.assertEqual(command.count("--resume"), 1)
        self.assertEqual(command[command.index("--resume") + 1], "bridge-session")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "session ID"):
                build_claude_command(
                    settings(root / "projects.json"),
                    policy(root),
                    "high",
                    "bad\nsession",
                )


class ReviewerCommandTests(unittest.TestCase):
    def test_never_emits_resume_even_though_kwarg_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(
                settings(root / "projects.json"),
                policy(root),
                "high",
                resume_session_id=None,
            )

        self.assertNotIn("--resume", command)

    def test_rejects_non_none_resume_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(AssertionError):
                build_reviewer_command(
                    settings(root / "projects.json"),
                    policy(root),
                    "high",
                    resume_session_id="anything",
                )

    def test_tools_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(settings(root / "projects.json"), policy(root), "high")

        self.assertIn("Read,Glob,Grep", command)
        self.assertNotIn("Read,Glob,Grep,Edit,Write", command)
        self.assertNotIn("Edit", command)
        self.assertNotIn("Write", command)

    def test_permission_mode_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(settings(root / "projects.json"), policy(root), "high")

        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")
        self.assertNotIn("acceptEdits", command)

    def test_preserves_disallowed_tool_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(settings(root / "projects.json"), policy(root), "high")

        self.assertIn("Bash", command)
        self.assertIn("WebFetch", command)
        self.assertIn("WebSearch", command)
        self.assertIn("mcp__*", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("--output-format", command)

    def test_rejects_ultracode_when_project_disallows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, allow_ultracode=False)
            with self.assertRaisesRegex(ValueError, "does not allow ultracode"):
                build_reviewer_command(settings(root / "projects.json"), project, "ultracode")

    def test_project_overrides_model_and_turn_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, claude_model="custom:model", claude_max_turns=17)
            command = build_reviewer_command(settings(root / "projects.json"), project, "high")

        self.assertEqual(command[command.index("--model") + 1], "custom:model")
        self.assertEqual(command[command.index("--max-turns") + 1], "17")

    def test_selected_effort_is_passed_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(settings(root / "projects.json"), policy(root), "ultracode")

        self.assertEqual(command.count("--effort"), 1)
        self.assertEqual(command[command.index("--effort") + 1], "ultracode")


if __name__ == "__main__":
    unittest.main()
