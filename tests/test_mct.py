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


def policy_with_commands(
    root: Path,
    commands: list[dict[str, object]],
) -> ProjectPolicy:
    """Build a project with verification_commit + named verification commands.

    Mirrors the project shape used by ``test_checkpoints.py``'s tool-surface
    canary so the milestone acceptance-command branch can be exercised against
    real ``_run_command`` invocations (``{python}`` → ``sys.executable``, no
    network, no shell).
    """
    return policy(
        root,
        capabilities=[
            "repository_read",
            "claude_edit",
            "verification_commit",
        ],
        commit_roots=["."],
        verification_commands=commands,
        protected_paths=[".git", ".env"],
    )


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
            # M-CT-04: the terminal report partitions by closure.
            self.assertEqual(len(snapshot["completed_milestones"]), 1)
            self.assertEqual(snapshot["completed_milestones"][0]["id"], "m1")
            self.assertTrue(snapshot["completed_milestones"][0]["closed"])
            self.assertEqual(len(snapshot["remaining_milestones"]), 0)

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
            # M-CT-04: the unclosed milestone surfaces as remaining.
            self.assertEqual(len(snapshot["remaining_milestones"]), 1)
            self.assertEqual(snapshot["remaining_milestones"][0]["id"], "m1")
            self.assertFalse(snapshot["remaining_milestones"][0]["closed"])
            self.assertEqual(len(snapshot["completed_milestones"]), 0)

        asyncio.run(exercise())

    def test_milestone_closes_when_acceptance_command_passes(self) -> None:
        """M-CT-04: a passing acceptance command closes the milestone and it
        appears in ``completed_milestones`` (not ``remaining_milestones``)."""

        async def exercise() -> None:
            project = policy_with_commands(
                self.root,
                [
                    {
                        "id": "accept",
                        "argv": ["{python}", "-c", "import sys; sys.exit(0)"],
                    }
                ],
            )
            executor = MilestoneExecutor("docs/feature.md")
            manager = DevelopmentJobManager(
                self.settings, self.locks, executor, projects=(project,)
            )
            milestone = {
                "id": "m1",
                "title": "Feature docs",
                "acceptance_paths": ["docs/feature.md"],
                "acceptance_command_id": "accept",
            }
            started = await manager.start(project, "work", "high")
            job_id = str(started["job_id"])
            manager._jobs[job_id].milestones = [milestone]
            snapshot = await wait_for_status(
                manager, project, job_id, {"completed"}
            )
            # The milestone closed and is partitioned into completed.
            self.assertTrue(snapshot["milestones"][0].get("closed"))
            self.assertEqual(len(snapshot["completed_milestones"]), 1)
            self.assertEqual(snapshot["completed_milestones"][0]["id"], "m1")
            self.assertTrue(snapshot["completed_milestones"][0]["closed"])
            self.assertIn(
                "closed_at", snapshot["completed_milestones"][0]
            )
            self.assertEqual(len(snapshot["remaining_milestones"]), 0)
            # The redacted summary must not leak acceptance wiring.
            self.assertNotIn(
                "acceptance_paths", snapshot["completed_milestones"][0]
            )
            self.assertNotIn(
                "acceptance_command_id", snapshot["completed_milestones"][0]
            )

        asyncio.run(exercise())

    def test_milestone_stays_open_when_acceptance_command_fails(self) -> None:
        """M-CT-04: a failing acceptance command leaves the milestone open and
        it appears in ``remaining_milestones`` (not ``completed_milestones``)."""

        async def exercise() -> None:
            project = policy_with_commands(
                self.root,
                [
                    {
                        "id": "accept",
                        "argv": ["{python}", "-c", "import sys; sys.exit(1)"],
                    }
                ],
            )
            executor = MilestoneExecutor("docs/feature.md")
            manager = DevelopmentJobManager(
                self.settings, self.locks, executor, projects=(project,)
            )
            milestone = {
                "id": "m1",
                "title": "Feature docs",
                "acceptance_paths": ["docs/feature.md"],
                "acceptance_command_id": "accept",
            }
            started = await manager.start(project, "work", "high")
            job_id = str(started["job_id"])
            manager._jobs[job_id].milestones = [milestone]
            snapshot = await wait_for_status(
                manager, project, job_id, {"completed"}
            )
            # The command failed, so the milestone did not close.
            self.assertNotIn("closed", snapshot["milestones"][0])
            self.assertEqual(len(snapshot["remaining_milestones"]), 1)
            self.assertEqual(snapshot["remaining_milestones"][0]["id"], "m1")
            self.assertFalse(snapshot["remaining_milestones"][0]["closed"])
            self.assertEqual(len(snapshot["completed_milestones"]), 0)

        asyncio.run(exercise())

    def test_milestone_stays_open_when_acceptance_command_id_unknown(self) -> None:
        """M-CT-04: an unknown ``acceptance_command_id`` leaves the milestone
        open (no fabricated acceptance) and it appears in
        ``remaining_milestones``."""

        async def exercise() -> None:
            project = policy_with_commands(
                self.root,
                [
                    {
                        "id": "accept",
                        "argv": ["{python}", "-c", "import sys; sys.exit(0)"],
                    }
                ],
            )
            executor = MilestoneExecutor("docs/feature.md")
            manager = DevelopmentJobManager(
                self.settings, self.locks, executor, projects=(project,)
            )
            milestone = {
                "id": "m1",
                "title": "Feature docs",
                "acceptance_paths": ["docs/feature.md"],
                "acceptance_command_id": "nonexistent",
            }
            started = await manager.start(project, "work", "high")
            job_id = str(started["job_id"])
            manager._jobs[job_id].milestones = [milestone]
            snapshot = await wait_for_status(
                manager, project, job_id, {"completed"}
            )
            # The command id is unknown, so the milestone did not close.
            self.assertNotIn("closed", snapshot["milestones"][0])
            self.assertEqual(len(snapshot["remaining_milestones"]), 1)
            self.assertEqual(snapshot["remaining_milestones"][0]["id"], "m1")
            self.assertFalse(snapshot["remaining_milestones"][0]["closed"])
            self.assertEqual(len(snapshot["completed_milestones"]), 0)

        asyncio.run(exercise())


class WallTimeBudgetExecutor:
    """Returns a max_turns slice with a large ``wall_seconds`` so the cumulative
    wall-time ceiling trips across multiple slices.

    Each call writes a new file so the progress-gated extension path keeps the
    slice loop alive (the job must not pause on no-progress before the wall
    ceiling fires). The executor reports ``wall_seconds`` directly so the test
    is deterministic and does not depend on real elapsed time.
    """

    def __init__(self, wall_seconds: float, num_turns: int = 5) -> None:
        self.wall_seconds = wall_seconds
        self.num_turns = num_turns
        self.calls: list[dict[str, Any]] = []

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
        n = len(self.calls) + 1
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / f"step{n}.txt").write_text(f"step {n}\n", encoding="utf-8")
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-wall",
            "result": f"slice {n}",
            "wall_seconds": self.wall_seconds,
        }


class MonotonicWallExecutor:
    """Sleeps briefly so the ``time.monotonic()`` fallback in ``_run`` measures
    real elapsed wall-time (the executor reports NO ``wall_seconds``)."""

    def __init__(self, delay: float, num_turns: int = 5) -> None:
        self.delay = delay
        self.num_turns = num_turns
        self.calls: list[dict[str, Any]] = []

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
        n = len(self.calls) + 1
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / f"step{n}.txt").write_text(f"step {n}\n", encoding="utf-8")
        await asyncio.sleep(self.delay)
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-monotonic",
            "result": f"slice {n}",
        }


def wall_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9101",
    }
    payload.update(env)
    return Settings.from_env(payload)


class MCT02WallTimeBudgetTests(unittest.TestCase):
    """M-CT-02: cumulative wall-time budget + compute-proxy.

    These tests exercise the cumulative wall-time ceiling with fake executors
    (no real Claude, no network), mirroring the patterns in
    ``test_checkpoints.py``.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.locks = ProjectLockManager()
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cumulative_wall_ceiling_pauses_with_wall_reason(self) -> None:
        """A fake executor reporting large ``wall_seconds`` trips the
        cumulative ceiling across multiple slices and pauses as
        PAUSED_BUDGET_EXHAUSTED with the wall pause_reason."""

        async def exercise() -> None:
            # 60s per slice, 100s ceiling → after 2 slices (120s) the ceiling
            # fires. Keep hard_turns/no-progress high so only the wall ceiling
            # can terminate the run.
            settings = wall_settings(
                self.base,
                CLAUDE_HARD_WALL_SECONDS="100",
                CLAUDE_HARD_TURNS="1000",
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
            )
            executor = WallTimeBudgetExecutor(wall_seconds=60.0, num_turns=5)
            manager = DevelopmentJobManager(
                settings, self.locks, executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager,
                self.project,
                job_id,
                {"paused_budget_exhausted"},
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertEqual(
                paused["pause_reason"], "cumulative_wall_budget_exhausted"
            )
            # Wall-time accumulated monotonically across slices.
            self.assertGreaterEqual(
                paused["budget"]["wall_seconds_used"], 100.0
            )
            self.assertEqual(paused["budget"]["hard_wall_seconds"], 100)
            # At least two slices ran before the ceiling fired.
            self.assertGreaterEqual(len(executor.calls), 2)

        asyncio.run(exercise())

    def test_wall_time_accumulates_monotonically_across_slices(self) -> None:
        """A multi-slice run accumulates wall-time monotonically; the snapshot
        exposes a non-decreasing ``wall_seconds_used`` that grows per slice."""

        async def exercise() -> None:
            # High ceiling so the job pauses on hard_turns (not wall), letting
            # us observe accumulation across several slices.
            settings = wall_settings(
                self.base,
                CLAUDE_HARD_WALL_SECONDS="100000",
                CLAUDE_HARD_TURNS="40",
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
            )
            executor = WallTimeBudgetExecutor(wall_seconds=10.0, num_turns=20)
            manager = DevelopmentJobManager(
                settings, self.locks, executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager,
                self.project,
                job_id,
                {"paused_budget_exhausted"},
            )
            # Multiple slices ran (extension-granted), wall-time grew.
            self.assertGreaterEqual(len(executor.calls), 2)
            self.assertGreaterEqual(
                paused["budget"]["wall_seconds_used"], 20.0
            )
            # Each slice reported exactly 10s; cumulative is a whole multiple.
            self.assertEqual(
                paused["budget"]["wall_seconds_used"],
                10.0 * len(executor.calls),
            )

        asyncio.run(exercise())

    def test_disabled_ceiling_never_pauses_on_wall_time(self) -> None:
        """A job under a zero/disabled ceiling never pauses on wall-time,
        even when the executor reports large ``wall_seconds``. The default
        (unset) ceiling is 0, so existing projects are unaffected."""

        async def exercise() -> None:
            # No CLAUDE_HARD_WALL_SECONDS → 0 (disabled). The job pauses on
            # hard_turns, NOT on wall-time.
            settings = wall_settings(
                self.base,
                CLAUDE_HARD_TURNS="40",
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="10",
            )
            self.assertEqual(settings.claude_hard_wall_seconds, 0)
            executor = WallTimeBudgetExecutor(
                wall_seconds=99999.0, num_turns=20
            )
            manager = DevelopmentJobManager(
                settings, self.locks, executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager,
                self.project,
                job_id,
                {"paused_budget_exhausted"},
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            # The pause reason is the hard-turn cap, NOT the wall ceiling.
            self.assertIn("hard turn cap", str(paused["pause_reason"]))
            self.assertNotEqual(
                paused["pause_reason"], "cumulative_wall_budget_exhausted"
            )
            # Wall-time was still accumulated (just not ceiling-checked).
            self.assertGreater(
                paused["budget"]["wall_seconds_used"], 0.0
            )
            self.assertEqual(paused["budget"]["hard_wall_seconds"], 0)

        asyncio.run(exercise())

    def test_monotonic_fallback_measures_wall_time(self) -> None:
        """An executor that reports NO ``wall_seconds`` still accumulates
        wall-time via the ``time.monotonic()`` fallback in ``_run``."""

        async def exercise() -> None:
            # 0.6s per slice, 1s ceiling → after 2 slices (1.2s) the ceiling
            # fires. Plenty of extension budget so the wall ceiling trips first.
            settings = wall_settings(
                self.base,
                CLAUDE_HARD_WALL_SECONDS="1",
                CLAUDE_HARD_TURNS="1000",
                CLAUDE_MAX_NO_PROGRESS_TURNS="100",
                CLAUDE_MAX_EXTENSIONS="100",
            )
            executor = MonotonicWallExecutor(delay=0.6, num_turns=5)
            manager = DevelopmentJobManager(
                settings, self.locks, executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager,
                self.project,
                job_id,
                {"paused_budget_exhausted"},
            )
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertEqual(
                paused["pause_reason"], "cumulative_wall_budget_exhausted"
            )
            # The monotonic fallback measured real elapsed time (> 0).
            self.assertGreater(
                paused["budget"]["wall_seconds_used"], 0.0
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
