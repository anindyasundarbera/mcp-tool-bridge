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
        self.assertTrue(settings.projects_config_path.is_absolute())
        self.assertEqual(settings.projects_config_path.name, "projects.json")

    def test_rejects_non_positive_limits(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "greater than zero"):
            Settings.from_env({"CLAUDE_MAX_TURNS": "0"})
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


def _policy(**overrides: object) -> ProjectPolicy:
    return ProjectPolicy(**project_payload(Path("/tmp/repo"), **overrides))


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


if __name__ == "__main__":
    unittest.main()
