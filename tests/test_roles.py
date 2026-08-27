"""M-GV-02 named read-only roles: Planner / Evidence-officer / Supervisor.

Each role is a capability-gated read-only invocation built in ``claude.py``,
mirroring ``build_reviewer_command``: a one-shot, non-resuming Claude launch
command whose toolset is strictly ``Read,Glob,Grep`` (no Edit/Write, no shell,
no web, no secondary MCP tools) and whose role-specific system prompt is passed
as a standalone ``--append-system-prompt`` argument. The parse helpers turn a
role's Claude output into a structured result and fail closed on malformed
output: the Planner emits a milestone list, the Evidence-officer emits a verdict
(and a fingerprint mismatch is a hard fail), and the Supervisor surfaces
no-progress.

Internal-first: no MCP tool and no ``ProjectCapability`` are added here, so the
tool-count canaries are untouched.
"""

import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_dev_mcp.claude import (
    _parse_output,
    build_evidence_officer_command,
    build_planner_command,
    build_supervisor_command,
    parse_evidence_verdict,
    parse_planner_output,
    parse_supervisor_recommendations,
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


#: (role, builder, expected prompt marker) — the three M-GV-02 roles.
ROLE_BUILDERS = [
    ("planner", build_planner_command, "You are the Planner"),
    ("evidence-officer", build_evidence_officer_command, "You are the Evidence-officer"),
    ("supervisor", build_supervisor_command, "You are the Supervisor"),
]


class RoleCommandTests(unittest.TestCase):
    def _command(
        self, builder: Callable[..., list[str]], root: Path, **overrides: Any
    ) -> list[str]:
        return builder(settings(root / "projects.json"), policy(root, **overrides), "high")

    def test_each_role_toolset_is_read_only(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    command = self._command(builder, Path(directory))
                self.assertIn("Read,Glob,Grep", command)
                self.assertNotIn("Read,Glob,Grep,Edit,Write", command)
                self.assertNotIn("Edit", command)
                self.assertNotIn("Write", command)

    def test_each_role_uses_default_permission_mode(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    command = self._command(builder, Path(directory))
                self.assertEqual(
                    command[command.index("--permission-mode") + 1], "default"
                )
                self.assertNotIn("acceptEdits", command)

    def test_each_role_preserves_disallowed_tool_boundary(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    command = self._command(builder, Path(directory))
                self.assertIn("Bash", command)
                self.assertIn("WebFetch", command)
                self.assertIn("WebSearch", command)
                self.assertIn("mcp__*", command)
                self.assertIn("--strict-mcp-config", command)
                self.assertIn("--disable-slash-commands", command)
                self.assertNotIn("bypassPermissions", command)

    def test_each_role_embeds_its_role_specific_system_prompt(self) -> None:
        for name, builder, marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    command = self._command(builder, Path(directory))
                self.assertIn("--append-system-prompt", command)
                prompt = command[command.index("--append-system-prompt") + 1]
                self.assertIn(marker, prompt)

    def test_each_role_is_non_resuming(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    command = self._command(builder, root)
                self.assertNotIn("--resume", command)
                with self.assertRaises(AssertionError):
                    builder(
                        settings(root / "projects.json"),
                        policy(root),
                        "high",
                        resume_session_id="anything",
                    )

    def test_each_role_rejects_ultracode_when_project_disallows_it(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    project = policy(root, allow_ultracode=False)
                    with self.assertRaisesRegex(ValueError, "does not allow ultracode"):
                        builder(settings(root / "projects.json"), project, "ultracode")

    def test_each_role_honors_project_model_and_turn_limit(self) -> None:
        for name, builder, _marker in ROLE_BUILDERS:
            with self.subTest(role=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    command = self._command(
                        builder, root, claude_model="custom:model", claude_max_turns=19
                    )
                self.assertEqual(command[command.index("--model") + 1], "custom:model")
                self.assertEqual(command[command.index("--max-turns") + 1], "19")

    def test_context_is_embedded_in_the_appended_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_planner_command(
                settings(root / "projects.json"),
                policy(root),
                "high",
                context="M-GV-02-R1: add named role command builders.",
            )
        prompt = command[command.index("--append-system-prompt") + 1]
        self.assertIn("M-GV-02-R1", prompt)
        self.assertIn("Context:", prompt)

    def test_context_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "limit"):
                build_planner_command(
                    settings(root / "projects.json"),
                    policy(root),
                    "high",
                    context="x" * 30_000,
                )

    def test_shared_launch_prefix_is_honored(self) -> None:
        # The role builders reuse the operator-owned review launch template.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_supervisor_command(
                settings(
                    root / "projects.json",
                    CLAUDE_REVIEW_LAUNCH_COMMAND="claude --model {model} --review -p",
                ),
                policy(root),
                "high",
            )
        self.assertEqual(command[0], "claude")
        self.assertIn("--review", command)
        self.assertNotIn("{model}", command)
        self.assertIn("Read,Glob,Grep", command)


def _envelope(result_text: str) -> dict:
    return {"type": "result", "result": result_text, "is_error": False}


class PlannerOutputTests(unittest.TestCase):
    def test_parses_a_milestone_list_through_the_result_envelope(self) -> None:
        text = json.dumps(
            {
                "milestones": [
                    {
                        "id": "M1",
                        "title": "Alpha",
                        "scope": "a/**",
                        "exit_criteria": "tests pass",
                    },
                    {
                        "id": "M2",
                        "title": "Beta",
                        "scope": "b/**",
                        "exit_criteria": "docs updated",
                    },
                ],
                "assumptions": ["no new deps"],
            }
        )
        stdout = json.dumps(_envelope(text))
        milestones = parse_planner_output(_parse_output(stdout, stream=False))
        self.assertEqual(len(milestones), 2)
        self.assertEqual(milestones[0]["id"], "M1")
        self.assertEqual(milestones[0]["title"], "Alpha")
        self.assertEqual(milestones[0]["scope"], "a/**")
        self.assertEqual(milestones[1]["exit_criteria"], "docs updated")

    def test_accepts_a_raw_string_result(self) -> None:
        text = json.dumps(
            {"milestones": [{"id": "M1", "title": "Alpha"}], "assumptions": []}
        )
        milestones = parse_planner_output(text)
        self.assertEqual(milestones[0]["id"], "M1")
        self.assertEqual(milestones[0]["title"], "Alpha")

    def test_rejects_output_without_a_milestone_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "milestones"):
            parse_planner_output(_envelope(json.dumps({"assumptions": []})))

    def test_rejects_non_json_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_planner_output(_envelope("not json"))

    def test_rejects_empty_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "no output"):
            parse_planner_output(_envelope(""))

    def test_rejects_a_milestone_missing_id_or_title(self) -> None:
        text = json.dumps(
            {"milestones": [{"scope": "a/**"}], "assumptions": []}
        )
        with self.assertRaisesRegex(ValueError, "id and a title"):
            parse_planner_output(_envelope(text))

    def test_rejects_an_unbounded_plan(self) -> None:
        text = json.dumps(
            {
                "milestones": [
                    {"id": f"M{i}", "title": f"T{i}"} for i in range(200)
                ],
                "assumptions": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "limit"):
            parse_planner_output(_envelope(text))


class EvidenceOfficerOutputTests(unittest.TestCase):
    def test_rejects_a_declared_fingerprint_mismatch(self) -> None:
        text = json.dumps(
            {
                "pass": False,
                "fingerprint_mismatch": True,
                "observed_fingerprint": "abc",
                "coverage_ok": True,
                "issues": ["fingerprint drifted from base"],
            }
        )
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            parse_evidence_verdict(_envelope(text), expected_fingerprint="abc")

    def test_rejects_observed_fingerprint_different_from_expected(self) -> None:
        text = json.dumps(
            {
                "pass": False,
                "fingerprint_mismatch": False,
                "observed_fingerprint": "different",
                "coverage_ok": True,
                "issues": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            parse_evidence_verdict(_envelope(text), expected_fingerprint="abc")

    def test_accepts_a_matching_fingerprint(self) -> None:
        text = json.dumps(
            {
                "pass": True,
                "fingerprint_mismatch": False,
                "observed_fingerprint": "abc",
                "coverage_ok": True,
                "issues": [],
            }
        )
        verdict = parse_evidence_verdict(_envelope(text), expected_fingerprint="abc")
        self.assertTrue(verdict["pass"])
        self.assertFalse(verdict["fingerprint_mismatch"])
        self.assertTrue(verdict["coverage_ok"])

    def test_accepts_a_verdict_without_an_observed_fingerprint(self) -> None:
        text = json.dumps(
            {"pass": True, "fingerprint_mismatch": False, "coverage_ok": True, "issues": []}
        )
        verdict = parse_evidence_verdict(_envelope(text), expected_fingerprint="abc")
        self.assertTrue(verdict["pass"])

    def test_rejects_malformed_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON"):
            parse_evidence_verdict(_envelope("garbage"), expected_fingerprint="abc")


class SupervisorOutputTests(unittest.TestCase):
    def test_flags_no_progress(self) -> None:
        text = json.dumps(
            {
                "no_progress": True,
                "status": "escalate",
                "recommendations": ["no new commits across 8 checkpoints"],
            }
        )
        parsed = parse_supervisor_recommendations(_envelope(text))
        self.assertTrue(parsed["no_progress"])
        self.assertEqual(parsed["status"], "escalate")
        self.assertEqual(parsed["recommendations"], ["no new commits across 8 checkpoints"])

    def test_returns_continue_when_progressing(self) -> None:
        text = json.dumps(
            {"no_progress": False, "status": "continue", "recommendations": []}
        )
        parsed = parse_supervisor_recommendations(_envelope(text))
        self.assertFalse(parsed["no_progress"])
        self.assertEqual(parsed["status"], "continue")

    def test_accepts_every_valid_status(self) -> None:
        for status in ("continue", "escalate", "narrow", "replace"):
            with self.subTest(status=status):
                text = json.dumps(
                    {"no_progress": False, "status": status, "recommendations": []}
                )
                parsed = parse_supervisor_recommendations(_envelope(text))
                self.assertEqual(parsed["status"], status)

    def test_rejects_an_unknown_status(self) -> None:
        text = json.dumps(
            {"no_progress": False, "status": "panic", "recommendations": []}
        )
        with self.assertRaisesRegex(ValueError, "status"):
            parse_supervisor_recommendations(_envelope(text))

    def test_rejects_a_missing_recommendations_list(self) -> None:
        text = json.dumps({"no_progress": False, "status": "continue"})
        with self.assertRaisesRegex(ValueError, "recommendations"):
            parse_supervisor_recommendations(_envelope(text))


if __name__ == "__main__":
    unittest.main()
