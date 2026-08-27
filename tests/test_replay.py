"""M-ROADMAP: Deterministic replay — the read-only ``replay_verification`` tool.

Tests cover the happy path (a recorded verification replays reproducibly), a
changed command definition surfacing as a digest mismatch, unknown/wrong-id
rejection, the capability gate (dormant by default, opt-in via the
``replay_verification`` project capability), the tool being present in the
schema even when dormant, and the annotation lock. Uses fake executors (no
network).
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import VerificationCommitManager, VerificationError

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, project, wait_completed


def replay_settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
            "MAX_VERIFICATION_OUTPUT_BYTES": "1024",
        }
    )


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add source"], check=True
        )
        self.settings = replay_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def prepare_verified(
        self,
        policy: ProjectPolicy,
    ) -> tuple[VerificationCommitManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(lambda root: (root / "README.md").write_text(
                "# Candidate\n", encoding="utf-8"
            )),
            projects=(policy,),
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_completed(jobs, policy, str(started["job_id"]))
        verified = await verification.verify(
            policy, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        return verification, verified

    def test_replay_reproduces_recorded_verification(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, verified = await self.prepare_verified(policy)
            replay = await verification.replay(
                policy, str(verified["verification_id"])
            )
            self.assertTrue(replay["reproducible"], replay)
            self.assertEqual(
                replay["environment_digest"]["recorded"],
                verified["environment_digest"],
            )
            self.assertTrue(replay["environment_digest"]["match"])
            self.assertEqual(len(replay["commands"]), 1)
            command = replay["commands"][0]
            self.assertEqual(command["command_id"], "core")
            self.assertTrue(command["reconstructed"])
            self.assertTrue(command["digest_match"])
            self.assertTrue(command["result_match"])
            self.assertEqual(command["replayed_status"], "passed")
            self.assertEqual(command["replayed_exit_code"], 0)

        asyncio.run(exercise())

    def test_replay_surfaces_changed_command_definition(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, verified = await self.prepare_verified(policy)
            # The project's verification command definition changes after the
            # verification was recorded (same id, different argv).
            changed = project(
                self.root,
                ["{python}", "-c", "print('changed definition')"],
            )
            replay = await verification.replay(
                changed, str(verified["verification_id"])
            )
            self.assertFalse(replay["reproducible"], replay)
            command = replay["commands"][0]
            self.assertTrue(command["reconstructed"])
            self.assertFalse(command["digest_match"])
            self.assertFalse(command["result_match"])
            self.assertIsNone(command["replayed_status"])
            self.assertFalse(replay["environment_digest"]["match"])
            self.assertTrue(replay["notes"])

        asyncio.run(exercise())

    def test_replay_rejects_unknown_verification_id(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, _ = await self.prepare_verified(policy)
            with self.assertRaises(VerificationError):
                await verification.replay(policy, "0" * 32)

        asyncio.run(exercise())

    def test_replay_rejects_wrong_project(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, verified = await self.prepare_verified(policy)
            other = project(
                self.root, ["{python}", "-c", "print('ok')"], id="other-project"
            )
            with self.assertRaises(VerificationError):
                await verification.replay(other, str(verified["verification_id"]))

        asyncio.run(exercise())


class ReplayCapabilityTests(unittest.TestCase):
    """Tool gating: replay_verification is registered but runtime-gated."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _server(self, policy: ProjectPolicy) -> Any:
        from local_dev_mcp.registry import ProjectRegistry
        from local_dev_mcp.server import create_server

        return create_server(
            replay_settings(self.base),
            ProjectRegistry({policy.id: policy}),
        )

    def test_replay_tool_present_in_schema(self) -> None:
        async def exercise() -> None:
            policy = project(
                self.root,
                ["{python}", "-c", "print('ok')"],
                capabilities=["repository_read", "claude_edit", "verification_commit"],
            )
            server = self._server(policy)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertIn("replay_verification", tools)

        asyncio.run(exercise())

    def test_replay_is_capability_gated_via_server(self) -> None:
        async def exercise() -> None:
            from fastmcp.exceptions import ToolError

            policy = project(
                self.root,
                ["{python}", "-c", "print('ok')"],
                capabilities=["repository_read", "claude_edit", "verification_commit"],
            )
            server = self._server(policy)
            with self.assertRaises(ToolError):
                await server.call_tool(
                    "replay_verification",
                    {
                        "project_id": "test-project",
                        "verification_id": "0" * 32,
                    },
                )

        asyncio.run(exercise())

    def test_replay_capability_requires_verification_commit_in_policy(self) -> None:
        with self.assertRaises(ValueError):
            ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "T",
                    "description": "T",
                    "root": str(self.root),
                    "capabilities": [
                        "repository_read",
                        "claude_edit",
                        ProjectCapability.REPLAY_VERIFICATION.value,
                    ],
                }
            )

    def test_replay_annotation_lock(self) -> None:
        async def exercise() -> None:
            policy = project(
                self.root,
                ["{python}", "-c", "print('ok')"],
                capabilities=[
                    "repository_read",
                    "claude_edit",
                    "verification_commit",
                    "replay_verification",
                ],
            )
            server = self._server(policy)
            tools = {tool.name: tool for tool in await server.list_tools()}
            tool = tools["replay_verification"]
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.destructiveHint)
            self.assertTrue(tool.annotations.idempotentHint)
            self.assertFalse(tool.annotations.openWorldHint)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
