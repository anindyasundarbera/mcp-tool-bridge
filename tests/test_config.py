import unittest
from pathlib import Path

from pydantic import ValidationError

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy

from tests.support import project_payload


class SettingsTests(unittest.TestCase):
    def test_defaults_and_relative_registry_resolution(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual(settings.claude_model, "glm-5.2:cloud")
        self.assertEqual(settings.mcp_port, 8000)
        self.assertEqual(settings.max_markdown_file_bytes, 1048576)
        self.assertEqual(settings.max_markdown_range_lines, 1000)
        self.assertEqual(settings.max_markdown_write_bytes, 1048576)
        self.assertEqual(settings.max_markdown_diff_bytes, 262144)
        self.assertEqual(settings.job_worktree_root.name, "worktrees")
        self.assertEqual(settings.git_command_timeout_seconds, 30)
        self.assertEqual(settings.candidate_mcp_port, 8001)
        self.assertEqual(settings.candidate_health_timeout_seconds, 30)
        self.assertEqual(settings.verification_command_timeout_seconds, 600)
        self.assertEqual(settings.max_verification_output_bytes, 1048576)
        self.assertEqual(settings.max_commit_message_chars, 500)
        self.assertEqual(settings.verification_deterministic_repetitions, 1)
        self.assertEqual(settings.max_delegations_per_job, 3)
        self.assertFalse(settings.reader_injection_defense)
        self.assertTrue(settings.projects_config_path.is_absolute())
        self.assertEqual(settings.projects_config_path.name, "projects.json")
        # The default launch command is the literal tuple, byte-identical to the
        # previous hardcoded argv, so the out-of-the-box behavior is unchanged.
        self.assertEqual(
            settings.claude_launch_command,
            ("ollama", "launch", "claude", "--model", "{model}", "--yes", "--", "-p"),
        )
        self.assertEqual(
            settings.claude_review_launch_command,
            ("ollama", "launch", "claude", "--model", "{model}", "--yes", "--", "-p"),
        )
        self.assertEqual(settings.claude_default_effort, "high")

    def test_reader_injection_defense_flag(self) -> None:
        self.assertFalse(Settings.from_env({}).reader_injection_defense)
        self.assertTrue(
            Settings.from_env({"READER_INJECTION_DEFENSE": "1"}).reader_injection_defense
        )
        self.assertTrue(
            Settings.from_env({"READER_INJECTION_DEFENSE": "true"}).reader_injection_defense
        )
        self.assertFalse(
            Settings.from_env({"READER_INJECTION_DEFENSE": "0"}).reader_injection_defense
        )

    def test_rejects_non_positive_limits(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"CLAUDE_MAX_TURNS": "0"})

    def test_require_apex_grounding_flag(self) -> None:
        # M-GV-03: the global REQUIRE_APEX_GROUNDING opt-in flag defaults off
        # and is set by a truthy env var.
        self.assertFalse(Settings.from_env({}).require_apex_grounding)
        self.assertTrue(
            Settings.from_env({"REQUIRE_APEX_GROUNDING": "1"}).require_apex_grounding
        )
        self.assertTrue(
            Settings.from_env({"REQUIRE_APEX_GROUNDING": "true"}).require_apex_grounding
        )
        self.assertFalse(
            Settings.from_env({"REQUIRE_APEX_GROUNDING": "0"}).require_apex_grounding
        )
        with self.assertRaisesRegex(RuntimeError, "MAX_MARKDOWN_WRITE_BYTES"):
            Settings.from_env({"MAX_MARKDOWN_WRITE_BYTES": "not-an-integer"})
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"MAX_MARKDOWN_DIFF_BYTES": "0"})
        with self.assertRaisesRegex(RuntimeError, "must differ"):
            Settings.from_env({"MCP_PORT": "9000", "CANDIDATE_MCP_PORT": "9000"})
        with self.assertRaisesRegex(RuntimeError, "at most 65535"):
            Settings.from_env({"CANDIDATE_MCP_PORT": "65536"})
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"VERIFICATION_COMMAND_TIMEOUT_SECONDS": "0"})
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"MAX_VERIFICATION_OUTPUT_BYTES": "0"})
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"MAX_COMMIT_MESSAGE_CHARS": "0"})

    def test_accepts_absolute_config_path(self) -> None:
        settings = Settings.from_env({"PROJECTS_CONFIG_PATH": "/tmp/projects.json"})
        self.assertEqual(settings.projects_config_path, Path("/tmp/projects.json"))


class LaunchCommandSettingsTests(unittest.TestCase):
    def test_custom_launch_command_shlex_split(self) -> None:
        settings = Settings.from_env(
            {"CLAUDE_LAUNCH_COMMAND": "claude --model {model} -p"}
        )
        self.assertEqual(
            settings.claude_launch_command,
            ("claude", "--model", "{model}", "-p"),
        )

    def test_review_launch_command_is_independent(self) -> None:
        settings = Settings.from_env(
            {
                "CLAUDE_LAUNCH_COMMAND": "claude --model {model} -p",
                "CLAUDE_REVIEW_LAUNCH_COMMAND": "claude --model {model} --review -p",
            }
        )
        self.assertEqual(settings.claude_launch_command[-1], "-p")
        self.assertIn("--review", settings.claude_review_launch_command)

    def test_rejects_launch_command_without_model_token(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "standalone '\\{model\\}'"):
            Settings.from_env({"CLAUDE_LAUNCH_COMMAND": "claude -p"})

    def test_rejects_model_substring_token(self) -> None:
        # ``prefix{model}`` is not a standalone token and must be rejected so the
        # bridge never silently emits a literal ``prefix{model}`` to the runtime.
        with self.assertRaisesRegex(RuntimeError, "standalone '\\{model\\}'"):
            Settings.from_env({"CLAUDE_LAUNCH_COMMAND": "claude --model prefix{model} -p"})

    def test_rejects_empty_launch_command(self) -> None:
        # Whitespace-only splits to zero tokens.
        with self.assertRaisesRegex(RuntimeError, "must not be empty"):
            Settings.from_env({"CLAUDE_LAUNCH_COMMAND": "   "})

    def test_rejects_empty_token_in_launch_command(self) -> None:
        # A quoted empty argument is an empty token, which is also rejected.
        with self.assertRaisesRegex(RuntimeError, "empty tokens"):
            Settings.from_env({"CLAUDE_LAUNCH_COMMAND": "claude '' {model}"})

    def test_rejects_too_many_tokens(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "at most 16 tokens"):
            Settings.from_env(
                {"CLAUDE_LAUNCH_COMMAND": " ".join(["x"] * 17) + " {model}"}
            )

    def test_empty_string_falls_back_to_default(self) -> None:
        settings = Settings.from_env({"CLAUDE_LAUNCH_COMMAND": ""})
        self.assertEqual(settings.claude_launch_command[0], "ollama")


class DefaultEffortSettingsTests(unittest.TestCase):
    def test_claude_default_effort_validation(self) -> None:
        self.assertEqual(
            Settings.from_env({"CLAUDE_DEFAULT_EFFORT": "high"}).claude_default_effort,
            "high",
        )
        self.assertEqual(
            Settings.from_env({"CLAUDE_DEFAULT_EFFORT": "ultracode"}).claude_default_effort,
            "ultracode",
        )
        with self.assertRaisesRegex(RuntimeError, "must be 'high' or 'ultracode'"):
            Settings.from_env({"CLAUDE_DEFAULT_EFFORT": "bogus"})
        # Empty/absent falls back to the default.
        self.assertEqual(
            Settings.from_env({"CLAUDE_DEFAULT_EFFORT": ""}).claude_default_effort,
            "high",
        )


def _policy(**overrides: object) -> ProjectPolicy:
    return ProjectPolicy(**project_payload(Path("/tmp/repo"), **overrides))


class GroundingAndSpecGatePolicyTests(unittest.TestCase):
    """M-GV-03 / M-GV-04 policy-level validation of external grounding and
    the ratified spec commit."""

    def test_grounding_flag_requires_a_ref(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires apex_authority_ref"):
            _policy(require_apex_grounding=True)

    def test_grounding_flag_with_a_ref_is_accepted(self) -> None:
        policy = _policy(
            require_apex_grounding=True,
            apex_authority_ref=f"docs/spec.md@{'a' * 40}",
        )
        self.assertEqual(policy.apex_authority_ref, f"docs/spec.md@{'a' * 40}")

    def test_ratified_spec_commit_must_be_a_full_sha(self) -> None:
        for value in ("abc", "abc123", "g" * 40, "a" * 39):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValidationError, "40-character git SHA"
                ):
                    _policy(ratified_spec_commit=value)
        with self.assertRaisesRegex(ValidationError, "must not be empty"):
            _policy(ratified_spec_commit="")

    def test_ratified_spec_commit_is_accepted(self) -> None:
        policy = _policy(ratified_spec_commit="b" * 40)
        self.assertEqual(policy.ratified_spec_commit, "b" * 40)


class CandidateReviewPolicyTests(unittest.TestCase):
    def test_candidate_review_alone_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "candidate_review requires claude_edit"):
            _policy(
                capabilities=["repository_read", "candidate_review"],
            )

    def test_candidate_review_with_claude_edit_is_accepted(self) -> None:
        policy = _policy(
            capabilities=[
                "repository_read",
                "claude_edit",
                "candidate_review",
            ],
        )
        self.assertIn("candidate_review", {c.value for c in policy.capabilities})

    def test_requires_review_without_verification_commit_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "requires_review requires verification_commit"
        ):
            _policy(
                capabilities=["repository_read", "claude_edit"],
                requires_review=True,
            )

    def test_requires_review_without_candidate_review_is_rejected(self) -> None:
        # requires_review must imply candidate_review: without it no review can
        # ever be recorded, so the commit gate would deadlock the project.
        with self.assertRaisesRegex(
            ValidationError, "requires_review requires candidate_review"
        ):
            _policy(
                capabilities=[
                    "repository_read",
                    "claude_edit",
                    "verification_commit",
                ],
                commit_roots=["."],
                verification_commands=[
                    {"id": "core", "argv": ["{python}", "-m", "unittest"]},
                ],
                requires_review=True,
            )

    def test_requires_review_with_verification_commit_and_candidate_review_is_accepted(
        self,
    ) -> None:
        policy = _policy(
            capabilities=[
                "repository_read",
                "claude_edit",
                "verification_commit",
                "candidate_review",
            ],
            commit_roots=["."],
            verification_commands=[
                {"id": "core", "argv": ["{python}", "-m", "unittest"]},
            ],
            requires_review=True,
        )
        self.assertTrue(policy.requires_review)

    def test_allow_bridge_reviewer_without_candidate_review_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "allow_bridge_reviewer requires candidate_review"
        ):
            _policy(
                capabilities=["repository_read", "claude_edit"],
                allow_bridge_reviewer=True,
            )

    def test_allow_bridge_reviewer_with_candidate_review_is_accepted(self) -> None:
        policy = _policy(
            capabilities=[
                "repository_read",
                "claude_edit",
                "candidate_review",
            ],
            allow_bridge_reviewer=True,
        )
        self.assertTrue(policy.allow_bridge_reviewer)

    def test_existing_style_project_still_validates(self) -> None:
        policy = _policy()
        self.assertFalse(policy.requires_review)
        self.assertFalse(policy.allow_bridge_reviewer)
        self.assertNotIn("candidate_review", {c.value for c in policy.capabilities})

    def test_push_development_commit_without_verification_commit_is_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            ValidationError, "push_development_commit requires verification_commit"
        ):
            _policy(
                capabilities=[
                    "repository_read",
                    "claude_edit",
                    "push_development_commit",
                ],
            )

    def test_push_development_commit_with_verification_commit_is_accepted(
        self,
    ) -> None:
        policy = _policy(
            capabilities=[
                "repository_read",
                "claude_edit",
                "verification_commit",
                "push_development_commit",
            ],
            commit_roots=["."],
            verification_commands=[
                {"id": "core", "argv": ["{python}", "-m", "unittest"]},
            ],
        )
        self.assertIn(
            "push_development_commit", {c.value for c in policy.capabilities}
        )


def _runtime_command(**overrides: object) -> dict[str, object]:
    command: dict[str, object] = {"id": "integration", "argv": ["{python}", "-c", "1"]}
    command.update(overrides)
    return command


class RuntimeVerificationPolicyTests(unittest.TestCase):
    def test_runtime_verification_without_repository_read_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "runtime_verification requires repository_read"
        ):
            _policy(
                capabilities=["runtime_verification"],
                runtime_verification_commands=[_runtime_command()],
            )

    def test_runtime_verification_without_commands_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "runtime_verification requires at least one runtime verification command",
        ):
            _policy(capabilities=["repository_read", "runtime_verification"])

    def test_runtime_verification_with_repository_read_and_command_is_accepted(
        self,
    ) -> None:
        policy = _policy(
            capabilities=["repository_read", "runtime_verification"],
            runtime_verification_commands=[_runtime_command()],
        )
        self.assertIn(
            "runtime_verification", {c.value for c in policy.capabilities}
        )
        self.assertEqual(len(policy.runtime_verification_commands), 1)

    def test_duplicate_runtime_verification_command_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "runtime verification command IDs must be unique per project",
        ):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    _runtime_command(),
                    _runtime_command(),
                ],
            )

    def test_runtime_verification_shell_argv_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "runtime verification commands must not invoke a shell"
        ):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    {"id": "sh", "argv": ["bash", "-c", "true"]},
                ],
            )

    def test_runtime_verification_forbidden_env_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "environment key 'LD_PRELOAD' is forbidden",
        ):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    {
                        "id": "env",
                        "argv": ["{python}", "-c", "1"],
                        "environment": {"LD_PRELOAD": "/x/lib.so"},
                    },
                ],
            )

    def test_runtime_verification_bad_env_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "environment keys must be uppercase"
        ):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    {
                        "id": "env",
                        "argv": ["{python}", "-c", "1"],
                        "environment": {"lower-key": "value"},
                    },
                ],
            )

    def test_runtime_verification_invalid_execution_mode_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    {"id": "x", "argv": ["{python}", "-c", "1"], "execution_mode": "container"},
                ],
            )

    def test_runtime_verification_empty_argv_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "runtime verification argv must contain 1 through 64 arguments"
        ):
            _policy(
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[{"id": "x", "argv": []}],
            )


def _capture_scope(**overrides: object) -> dict[str, object]:
    scope: dict[str, object] = {
        "include_paths": ["AGENTS.md", "docs/**"],
        "protected_exclude_paths": [".reasonix/a.json"],
    }
    scope.update(overrides)
    return scope


class WorkingTreeCapturePolicyTests(unittest.TestCase):
    def test_capture_without_repository_read_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "working_tree_candidate_capture requires repository_read",
        ):
            _policy(
                capabilities=["claude_edit", "working_tree_candidate_capture"],
                working_tree_capture_scope=_capture_scope(),
            )

    def test_capture_without_pinned_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "working_tree_candidate_capture requires a pinned working_tree_capture_scope",
        ):
            _policy(
                capabilities=["repository_read", "working_tree_candidate_capture"],
            )

    def test_capture_with_repository_read_and_scope_is_accepted(self) -> None:
        policy = _policy(
            capabilities=["repository_read", "working_tree_candidate_capture"],
            commit_roots=["."],
            working_tree_capture_scope=_capture_scope(),
        )
        self.assertIn(
            "working_tree_candidate_capture",
            {c.value for c in policy.capabilities},
        )
        assert policy.working_tree_capture_scope is not None
        self.assertEqual(
            policy.working_tree_capture_scope.include_paths,
            ("AGENTS.md", "docs/**"),
        )

    def test_scope_requires_non_empty_include_paths(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "working_tree_capture_scope requires a non-empty include_paths",
        ):
            _policy(
                capabilities=["repository_read", "working_tree_candidate_capture"],
                working_tree_capture_scope=_capture_scope(include_paths=[]),
            )

    def test_scope_rejects_absolute_and_traversal_paths(self) -> None:
        for bad in ("/etc/passwd", "../escape", "a/../b", "docs/../secret.md"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                    ValidationError,
                    "capture scope paths must be non-empty relative paths without '..'",
                ):
                    _policy(
                        capabilities=[
                            "repository_read",
                            "working_tree_candidate_capture",
                        ],
                        working_tree_capture_scope=_capture_scope(
                            include_paths=[bad]
                        ),
                    )

    def test_scope_rejects_glob_not_in_final_segment(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "'\\*\\*' is allowed only as the final path segment"
        ):
            _policy(
                capabilities=["repository_read", "working_tree_candidate_capture"],
                working_tree_capture_scope=_capture_scope(
                    include_paths=["docs/**/x.md"]
                ),
            )

    def test_scope_rejects_duplicate_paths(self) -> None:
        with self.assertRaisesRegex(
            ValidationError, "capture scope paths must not contain duplicates"
        ):
            _policy(
                capabilities=["repository_read", "working_tree_candidate_capture"],
                working_tree_capture_scope=_capture_scope(
                    include_paths=["AGENTS.md", "AGENTS.md"]
                ),
            )

    def test_scope_rejects_string_paths_value(self) -> None:
        with self.assertRaises(ValidationError):
            _policy(
                capabilities=["repository_read", "working_tree_candidate_capture"],
                working_tree_capture_scope=_capture_scope(include_paths="AGENTS.md"),
            )


if __name__ == "__main__":
    unittest.main()
