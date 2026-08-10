import json
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.claude import (
    _parse_output,
    build_claude_command,
    build_reviewer_command,
)
from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy


def settings(config_path: Path, **env: str) -> Settings:
    base = {"PROJECTS_CONFIG_PATH": str(config_path)}
    base.update(env)
    return Settings.from_env(base)


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


class ContinuityCommandTests(unittest.TestCase):
    def test_max_turns_override_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, claude_max_turns=17)
            command = build_claude_command(
                settings(root / "projects.json"),
                project,
                "high",
                max_turns_override=44,
            )
        self.assertEqual(command[command.index("--max-turns") + 1], "44")

    def test_max_turns_override_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "must be positive"):
                build_claude_command(
                    settings(root / "projects.json"),
                    policy(root),
                    "high",
                    max_turns_override=0,
                )

    def test_default_uses_json_output_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json"), policy(root), "high"
            )
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertNotIn("--verbose", command)

    def test_stream_json_switches_format_and_adds_verbose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json", CLAUDE_STREAM_JSON="1"),
                policy(root),
                "high",
            )
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", command)
        # The restricted tool boundary is unchanged in stream mode.
        self.assertIn("Read,Glob,Grep,Edit,Write", command)
        self.assertNotIn("bypassPermissions", command)

    def test_reviewer_command_stays_json_in_stream_mode(self) -> None:
        # The reviewer is one-shot and never streams; the reviewer builder
        # ignores the stream flag entirely.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(
                settings(root / "projects.json", CLAUDE_STREAM_JSON="1"),
                policy(root),
                "high",
            )
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertNotIn("--verbose", command)


class ParseOutputTests(unittest.TestCase):
    def test_json_mode_parses_single_envelope(self) -> None:
        envelope = {"result": "done", "session_id": "s1", "num_turns": 3, "usage": {}}
        parsed = _parse_output(json.dumps(envelope), stream=False)
        self.assertEqual(parsed, envelope)

    def test_json_mode_falls_back_on_garbage(self) -> None:
        parsed = _parse_output("not json", stream=False)
        self.assertTrue(parsed["is_error"])
        self.assertEqual(parsed["result"], "not json")

    def test_stream_mode_extracts_result_envelope(self) -> None:
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant"},
            {"type": "assistant"},
            {"type": "user", "content": [{"type": "tool_result", "is_error": False}]},
            {
                "type": "result",
                "result": "finished",
                "session_id": "s1",
                "num_turns": 2,
                "subtype": "success",
                "is_error": False,
            },
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        parsed = _parse_output(stdout, stream=True)
        self.assertEqual(parsed["result"], "finished")
        self.assertEqual(parsed["session_id"], "s1")
        self.assertEqual(parsed["num_turns"], 2)

    def test_stream_mode_counts_turns_and_tool_failures(self) -> None:
        events = [
            {"type": "assistant"},
            {"type": "assistant"},
            {"type": "assistant"},
            {"type": "user", "content": [{"is_error": True}]},
            {"type": "user", "content": [{"is_error": True}]},
            {"type": "result", "result": "x", "subtype": "success"},
        ]
        parsed = _parse_output(
            "\n".join(json.dumps(e) for e in events), stream=True
        )
        # The result event omitted num_turns, so the parser derives it from
        # the assistant events it observed.
        self.assertEqual(parsed["num_turns"], 3)
        self.assertEqual(parsed["tool_failures"], 2)

    def test_stream_mode_skips_malformed_lines(self) -> None:
        stdout = "not json\n" + json.dumps({"type": "result", "result": "ok"}) + "\n"
        parsed = _parse_output(stdout, stream=True)
        self.assertEqual(parsed["result"], "ok")

    def test_stream_mode_without_result_event_is_error(self) -> None:
        stdout = json.dumps({"type": "assistant"})
        parsed = _parse_output(stdout, stream=True)
        self.assertTrue(parsed["is_error"])
        self.assertEqual(parsed["num_turns"], 1)


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
