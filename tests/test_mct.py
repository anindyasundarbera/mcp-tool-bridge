"""M-CT · MCPB-010 tail: worktree base fetch+pin, atomic milestones, and the
dormant BLOCKED_INPUT_REQUIRED state.

These tests exercise the continuity-tail additions with fake executors and a
local bare ``origin`` (no real Claude, no network), mirroring the patterns in
``tests/test_checkpoints.py`` and ``tests/test_sync.py``.
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager, JobStatus
from local_dev_mcp.models import Effort, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.worktrees import GitWorktreeManager, WorktreeError

from tests.support import initialize_git_repository
from tests.test_jobs import wait_for_status
from tests.test_sync import add_origin, advance_origin, init_repo


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "CANDIDATE_MCP_PORT": "9101",
        }
    )


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "M-CT tests",
        "root": str(root),
        "capabilities": ["repository_read", "claude_edit"],
    }
    data.update(overrides)
    return ProjectPolicy.model_validate(data)


class BlockedInputExecutor:
    """Returns a structured 'input required' signal on its first call."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls += 1
        return {
            "status": "failed",
            "result": (
                "I need the approved schema before continuing.\n"
                "<!-- bridge: blocked-input please provide the target schema -->"
            ),
            "session_id": "bridge-session-blocked",
            "num_turns": 2,
        }


class MilestoneExecutor:
    """Writes a milestone acceptance path, then completes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls = 0

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls += 1
        target = root / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("implemented\n", encoding="utf-8")
        return {
            "status": "completed",
            "project_id": project.id,
            "result": f"wrote {self.path}",
            "session_id": "bridge-session-milestone",
            "repository_after": {"clean": False, "working_tree": f"?? {self.path}"},
        }


class MCTWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        init_repo(self.root)
        self.settings = settings(self.base)
        self.manager = GitWorktreeManager(self.settings)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_requested_base_pins_worktree_to_fetched_remote_tip(self) -> None:
        bare = self.base / "origin.git"
        add_origin(self.root, bare)
        remote_tip = advance_origin(bare, self.base, "merged")

        worktree = asyncio.run(
            self.manager.create(self.project, "a" * 32, requested_base=remote_tip)
        )
        self.assertEqual(worktree.base_head, remote_tip)
        head = subprocess.run(
            ["git", "-C", str(worktree.path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head, remote_tip)
        # The canonical checkout is left untouched (still at the local HEAD).
        local_head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(local_head, remote_tip)

    def test_requested_base_rejects_non_remote_tip(self) -> None:
        bare = self.base / "origin.git"
        add_origin(self.root, bare)
        # Advance origin so the local HEAD is stale relative to the remote tip.
        advance_origin(bare, self.base, "merged")
        local_head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with self.assertRaisesRegex(WorktreeError, "not the current tip"):
            asyncio.run(
                self.manager.create(self.project, "b" * 32, requested_base=local_head)
            )

    def test_requested_base_requires_origin(self) -> None:
        # No origin remote configured: requested_base must fail closed.
        with self.assertRaisesRegex(WorktreeError, "origin"):
            asyncio.run(
                self.manager.create(self.project, "c" * 32, requested_base="0" * 40)
            )


class MCTJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = settings(self.base)
        self.locks = ProjectLockManager()
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self, executor: Any) -> DevelopmentJobManager:
        return DevelopmentJobManager(
            self.settings,
            self.locks,
            executor,
            projects=(self.project,),
        )

    def test_blocked_input_enters_resumable_dormant_state(self) -> None:
        async def exercise() -> None:
            manager = self._manager(BlockedInputExecutor())
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            snapshot = await wait_for_status(
                manager, self.project, job_id, {"blocked_input_required"}
            )
            self.assertEqual(snapshot["status"], "blocked_input_required")
            self.assertTrue(snapshot["resume_available"])
            self.assertIn(
                "please provide the target schema", snapshot["blockers"][0]
            )

        asyncio.run(exercise())

    def test_milestone_closes_when_acceptance_path_present(self) -> None:
        async def exercise() -> None:
            executor = MilestoneExecutor("docs/feature.md")
            manager = self._manager(executor)
            milestone = {
                "id": "m1",
                "title": "Feature docs",
                "acceptance_paths": ["docs/feature.md"],
            }
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            # Seed the milestone on the job before it runs to completion.
            job = manager._jobs[job_id]
            job.milestones = [milestone]
            snapshot = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertEqual(snapshot["milestones"][0]["closed"], True)
            self.assertIn("closed_at", snapshot["milestones"][0])

        asyncio.run(exercise())

    def test_milestone_stays_open_without_acceptance_path(self) -> None:
        async def exercise() -> None:
            executor = MilestoneExecutor("docs/feature.md")
            manager = self._manager(executor)
            milestone = {
                "id": "m1",
                "title": "Feature docs",
                "acceptance_paths": ["docs/other.md"],
            }
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            job = manager._jobs[job_id]
            job.milestones = [milestone]
            snapshot = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertNotIn("closed", snapshot["milestones"][0])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
