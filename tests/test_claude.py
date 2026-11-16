import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from local_dev_mcp.claude import (
    ClaudeExecutor,
    ClaudeExecutorAdapter,
    _parse_output,
    build_claude_command,
    build_reviewer_command,
)
from local_dev_mcp.config import Settings
from local_dev_mcp.executor_contract import (
    ExecutionRequest,
    ExecutorCheckpoint,
    ExecutorLifecycleState,
    ExecutorPort,
    ExecutorResourceLimits,
    NetworkMode,
)
from local_dev_mcp.models import ProjectPolicy
from tests.support import initialize_git_repository


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


class LaunchCommandTests(unittest.TestCase):
    _CURRENT_PREFIX = [
        "ollama",
        "launch",
        "claude",
        "--model",
        "glm-5.2:cloud",
        "--yes",
        "--",
        "-p",
    ]

    def test_default_launch_command_matches_current_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json"), policy(root), "high"
            )
        self.assertEqual(command[: len(self._CURRENT_PREFIX)], self._CURRENT_PREFIX)
        # The security tail is appended after the operator-owned prefix.
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("Read,Glob,Grep,Edit,Write", command)

    def test_custom_launch_command_substitutes_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json", CLAUDE_LAUNCH_COMMAND="claude --model {model} -p"),
                policy(root),
                "high",
            )
        # The custom prefix is honored, {model} is substituted, and the
        # bridge-enforced security tail is still appended unchanged.
        self.assertEqual(command[0], "claude")
        self.assertEqual(command[command.index("--model") + 1], "glm-5.2:cloud")
        self.assertNotIn("{model}", command)
        self.assertIn("--disallowedTools", command)
        self.assertIn("Bash", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--permission-mode", command)
        self.assertEqual(command[command.index("--effort") + 1], "high")

    def test_custom_launch_command_missing_model_token_rejected(self) -> None:
        # The {model} token is required at config-load time, so a template
        # without it never reaches the builder.
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "standalone '\\{model\\}'"):
                settings(
                    Path(directory) / "projects.json",
                    CLAUDE_LAUNCH_COMMAND="claude -p",
                )

    def test_custom_launch_command_honors_project_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = policy(root, claude_model="custom:model")
            command = build_claude_command(
                settings(root / "projects.json", CLAUDE_LAUNCH_COMMAND="claude --model {model} -p"),
                project,
                "high",
            )
        self.assertEqual(command[command.index("--model") + 1], "custom:model")

    def test_reviewer_launch_command_honored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_reviewer_command(
                settings(
                    root / "projects.json",
                    CLAUDE_REVIEW_LAUNCH_COMMAND="claude --model {model} --review -p",
                ),
                policy(root),
                "high",
            )
        self.assertEqual(command[0], "claude")
        self.assertIn("--review", command)
        # The reviewer security tail (read-only tools, default permission mode)
        # is still appended and unchanged.
        self.assertIn("Read,Glob,Grep", command)
        self.assertNotIn("Read,Glob,Grep,Edit,Write", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "default")

    def test_resume_segment_inserted_before_security_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = build_claude_command(
                settings(root / "projects.json"),
                policy(root),
                "high",
                "bridge-session",
            )
        self.assertEqual(command[command.index("--resume") + 1], "bridge-session")
        # --resume lands after the operator prefix, before --permission-mode.
        self.assertLess(command.index("--resume"), command.index("--permission-mode"))


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


class FakeClaudeExecutor(ClaudeExecutor):
    """Drop-in replacement for ClaudeExecutor used to test the adapter."""

    def __init__(self, outcome: dict[str, object] | None = None):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env(
                {"PROJECTS_CONFIG_PATH": str(Path(directory) / "projects.json")}
            )
        super().__init__(settings)
        self.outcome = outcome or {
            "status": "completed",
            "exit_code": 0,
            "num_turns": 2,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "session_id": "session-123",
            "repository_before": {"working_tree": ""},
            "repository_after": {"working_tree": " M src/foo.py\n"},
        }
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "project": project,
                "specification": specification,
                "effort": effort,
                "root": root,
                "resume_session_id": resume_session_id,
                "max_turns": max_turns,
                "execution_id": execution_id,
                "env": env,
            }
        )
        return self.outcome

    async def execute_review(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError

    async def cancel(self, execution_id: str, reason: str = "cancelled_by_caller") -> bool:
        self.cancelled.append(execution_id)
        return True


class WritingFakeClaudeExecutor(FakeClaudeExecutor):
    """Fake executor that actually writes a file so git status sees a change."""

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        root_path = root or project.root
        (root_path / "fake_output.txt").write_text("fake output\n", encoding="utf-8")
        outcome = dict(self.outcome)
        outcome["repository_before"] = {"working_tree": ""}
        outcome["repository_after"] = {"working_tree": "?? fake_output.txt\n"}
        outcome["session_id"] = resume_session_id or "session-123"
        return outcome


def _base_request(candidate: Path, canonical: Path | None = None, **kwargs: object) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id="exec-1",
        project_id="proj-1",
        job_id="job-1",
        candidate_root=candidate,
        canonical_root=canonical,
        mandate="do work",
        **kwargs,  # type: ignore[arg-type]
    )


class ClaudeExecutorAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.candidate = self.root / "candidate"
        self.canonical = self.root / "canonical"
        self.candidate.mkdir()
        self.canonical.mkdir()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(self, outcome: dict[str, object] | None = None) -> ClaudeExecutorAdapter:
        return ClaudeExecutorAdapter(FakeClaudeExecutor(outcome))

    def _fake(self, adapter: ClaudeExecutorAdapter) -> FakeClaudeExecutor:
        return cast(FakeClaudeExecutor, adapter._executor)

    async def test_adapter_implements_executor_port(self) -> None:
        adapter = self._adapter()
        self.assertIsInstance(adapter, ExecutorPort)

    async def test_capabilities_declared(self) -> None:
        adapter = self._adapter()
        caps = adapter.capabilities()
        self.assertEqual(caps.adapter_id, "claude")
        self.assertTrue(caps.supports_resume)
        self.assertTrue(caps.supports_streaming)
        self.assertTrue(caps.supports_cancellation)
        self.assertTrue(caps.supports_checkpoints)
        self.assertTrue(caps.supports_host_containment)
        self.assertEqual(caps.supported_network_modes, (NetworkMode.NONE,))
        self.assertEqual(caps.max_concurrent_executions, 1)

    async def test_health_reports_healthy(self) -> None:
        adapter = self._adapter()
        health = await adapter.health()
        self.assertTrue(health.healthy)
        self.assertEqual(health.adapter_id, "claude")
        self.assertIsNotNone(health.containment_available)

    async def test_start_returns_completed_result(self) -> None:
        adapter = self._adapter()
        request = _base_request(self.candidate, self.canonical)
        result = await adapter.start(request)
        self.assertEqual(result.state, ExecutorLifecycleState.COMPLETED)
        self.assertEqual(result.execution_id, "exec-1")
        self.assertEqual(result.adapter_id, "claude")
        self.assertFalse(result.grants_authority)
        self.assertEqual(result.modified_paths, ("src/foo.py",))
        self.assertEqual(result.resource_usage.turns_used, 2)
        self.assertEqual(result.resource_usage.exit_code, 0)
        self.assertIn("claude", result.vendor_diagnostics)

    async def test_start_emits_checkpoint(self) -> None:
        adapter = self._adapter()
        checkpoints: list[ExecutorCheckpoint] = []
        request = _base_request(self.candidate, self.canonical)
        result = await adapter.start(request, on_checkpoint=checkpoints.append)
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertGreaterEqual(len(result.checkpoints), 1)
        self.assertEqual(checkpoints[-1].execution_id, "exec-1")
        self.assertEqual(result.checkpoints[-1].resumable_token, "session-123")

    async def test_denied_tool_rejection_bash(self) -> None:
        adapter = self._adapter()
        request = _base_request(
            self.candidate,
            self.canonical,
            allowed_tools=("Read", "Bash"),
        )
        with self.assertRaisesRegex(ValueError, "Denied tool requested"):
            await adapter.start(request)

    async def test_unsupported_network_mode_rejection(self) -> None:
        adapter = self._adapter()
        request = _base_request(
            self.candidate,
            self.canonical,
            resource_limits=ExecutorResourceLimits(network_mode=NetworkMode.LOOPBACK_ONLY),
        )
        with self.assertRaisesRegex(ValueError, "Unsupported network mode"):
            await adapter.start(request)

    async def test_grants_authority_is_always_false(self) -> None:
        adapter = self._adapter()
        request = _base_request(self.candidate, self.canonical)
        result = await adapter.start(request)
        self.assertFalse(result.grants_authority)

    async def test_start_passes_env_projection(self) -> None:
        adapter = self._adapter()
        request = _base_request(
            self.candidate,
            self.canonical,
            env_projection={"MY_VAR": "value"},
        )
        await adapter.start(request)
        call = self._fake(adapter).calls[-1]
        env = call["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env.get("MY_VAR"), "value")
        self.assertIn("PATH", env)

    async def test_start_passes_resume_session_id(self) -> None:
        adapter = self._adapter()
        request = _base_request(
            self.candidate,
            self.canonical,
            resume_session_id="stored-session",
        )
        await adapter.start(request)
        call = self._fake(adapter).calls[-1]
        self.assertEqual(call["resume_session_id"], "stored-session")

    async def test_start_maps_timed_out(self) -> None:
        adapter = self._adapter(
            {
                "status": "timed_out",
                "exit_code": None,
                "repository_after": {"working_tree": ""},
            }
        )
        request = _base_request(self.candidate, self.canonical)
        result = await adapter.start(request)
        self.assertEqual(result.state, ExecutorLifecycleState.TIMED_OUT)
        self.assertEqual(result.error_code, "EXECUTION_TIMEOUT")
        self.assertFalse(result.grants_authority)

    async def test_start_maps_failed(self) -> None:
        adapter = self._adapter(
            {
                "status": "failed",
                "exit_code": 1,
                "error": "something went wrong",
                "repository_before": {"working_tree": ""},
                "repository_after": {"working_tree": ""},
            }
        )
        request = _base_request(self.candidate, self.canonical)
        result = await adapter.start(request)
        self.assertEqual(result.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(result.error_code, "CLAUDE_EXECUTION_FAILED")
        self.assertFalse(result.grants_authority)
        assert result.error_message is not None
        self.assertIn("something went wrong", result.error_message)

    async def test_start_propagates_isolation_validation_error(self) -> None:
        adapter = self._adapter()
        from unittest.mock import patch

        request = _base_request(self.candidate, self.canonical)
        with patch("local_dev_mcp.containment.validate_candidate_isolation") as mock_validate:
            mock_validate.side_effect = ValueError("isolation breach")
            with self.assertRaisesRegex(ValueError, "isolation breach"):
                await adapter.start(request)

    async def test_resume_uses_checkpoint_token(self) -> None:
        adapter = self._adapter()
        request = _base_request(self.candidate, self.canonical)
        checkpoint = ExecutorCheckpoint(
            checkpoint_id="chk-1",
            execution_id="exec-1",
            sequence=1,
            summary="",
            created_at="2026-11-16T00:00:00Z",
            resumable_token="token-from-checkpoint",
        )
        await adapter.resume(request, checkpoint)
        call = self._fake(adapter).calls[-1]
        self.assertEqual(call["resume_session_id"], "token-from-checkpoint")

    async def test_resume_prefers_explicit_session_id(self) -> None:
        adapter = self._adapter()
        request = _base_request(
            self.candidate,
            self.canonical,
            resume_session_id="explicit-session",
        )
        checkpoint = ExecutorCheckpoint(
            checkpoint_id="chk-1",
            execution_id="exec-1",
            sequence=1,
            summary="",
            created_at="2026-11-16T00:00:00Z",
            resumable_token="token-from-checkpoint",
        )
        await adapter.resume(request, checkpoint)
        call = self._fake(adapter).calls[-1]
        self.assertEqual(call["resume_session_id"], "explicit-session")

    async def test_cancel_delegates_to_executor(self) -> None:
        adapter = self._adapter()
        result = await adapter.cancel("exec-1", reason="test")
        self.assertTrue(result)
        self.assertEqual(self._fake(adapter).cancelled, ["exec-1"])


class ClaudeExecutorEnvironmentScrubbingTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_executor_default_minimal_env_scrubbing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = ClaudeExecutor(settings(root / "projects.json"))
            pol = policy(root)

            # Mock create_subprocess_exec to capture environment passed to subprocess
            passed_env: dict[str, str] = {}

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> Any:
                nonlocal passed_env
                if cmd and cmd[0] != "git":
                    passed_env = dict(kwargs.get("env") or {})

                class FakeProcess:
                    returncode = 0
                    pid = 12345

                    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
                        return (b'{"type": "result", "result": "done", "session_id": "s1"}', b'')

                    async def wait(self) -> int:
                        return 0

                return FakeProcess()

            from unittest.mock import patch
            with patch.dict(os.environ, {
                "SECRET_CANARY_TOKEN": "host-canary-secret",
                "OPENAI_API_KEY": "sk-openai-secret",
                "ANTHROPIC_API_KEY": "ant-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "NGROK_AUTHTOKEN": "ngrok-secret",
            }), patch("local_dev_mcp.claude.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
                res = await executor.execute(pol, "test specification", "high")
                self.assertEqual(res["status"], "completed")
                self.assertNotIn("SECRET_CANARY_TOKEN", passed_env)
                self.assertNotIn("OPENAI_API_KEY", passed_env)
                self.assertNotIn("ANTHROPIC_API_KEY", passed_env)
                self.assertNotIn("AWS_SECRET_ACCESS_KEY", passed_env)
                self.assertNotIn("NGROK_AUTHTOKEN", passed_env)
                self.assertEqual(passed_env.get("NO_COLOR"), "1")


class ClaudeExecutorCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_returns_false_when_no_active_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings.from_env(
                {"PROJECTS_CONFIG_PATH": str(Path(directory) / "projects.json")}
            )
            executor = ClaudeExecutor(settings)
            result = await executor.cancel("unknown-id")
            self.assertFalse(result)


class ClaudeExecutorAdapterConformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.candidate = self.workspace / "candidate"
        self.canonical = self.workspace / "canonical"
        self.candidate.mkdir()
        self.canonical.mkdir()
        initialize_git_repository(self.candidate)
        initialize_git_repository(self.canonical)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_adapter_passes_executor_conformance_suite(self) -> None:
        from local_dev_mcp.executor_contract import (
            run_executor_conformance_suite,
            run_executor_conformance_suite_sync,
        )

        adapter = ClaudeExecutorAdapter(WritingFakeClaudeExecutor())

        report = await run_executor_conformance_suite(lambda: adapter, self.workspace)
        self.assertTrue(report.passed, f"Conformance async failed: {report.failures}")

        report_sync = run_executor_conformance_suite_sync(lambda: adapter, self.workspace)
        self.assertTrue(report_sync.passed, f"Conformance sync failed: {report_sync.failures}")


if __name__ == "__main__":
    unittest.main()
