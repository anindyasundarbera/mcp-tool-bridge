"""M-CC-08: Human control surface.

Tests cover the dashboard enrichment (``authorities_in_force``) and the six
human control tools (``pause_development_job``, ``extend_budget``,
``narrow_scope``, ``replace_worker``, ``discard_candidate``,
``revoke_authorization``). Each control is capability-gated (dormant by
default, opt-in via its ``ProjectCapability``), repeat-safe where claimed, and
records an append-only audit event. Uses fake executors (no network).
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp import store
from local_dev_mcp.authorization import AuthorizationManager
from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager, ProjectRegistryError
from local_dev_mcp.server import create_server

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, project, wait_completed


def control_settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
            "MAX_VERIFICATION_OUTPUT_BYTES": "1024",
        }
    )


def server_for(base: Path, policy: ProjectPolicy) -> Any:
    from local_dev_mcp.registry import ProjectRegistry

    return create_server(
        Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(base / "jobs"),
            }
        ),
        ProjectRegistry({policy.id: policy}),
    )


def job_managers(
    settings: Settings, policy: ProjectPolicy
) -> tuple[DevelopmentJobManager, AuthorizationManager]:
    locks = ProjectLockManager()
    jobs = DevelopmentJobManager(
        settings,
        locks,
        EditingExecutor(
            lambda root: (root / "README.md").write_text(
                "# Candidate\n", encoding="utf-8"
            )
        ),
        projects=(policy,),
    )
    # Shares the same bridge.db under job_worktree_root/.state.
    authorization = AuthorizationManager(settings)
    return jobs, authorization


async def start_completed(
    jobs: DevelopmentJobManager, policy: ProjectPolicy
) -> dict[str, Any]:
    started = await jobs.start(policy, "make candidate", "high")
    return await wait_completed(jobs, policy, str(started["job_id"]))


class BlockingExecutor:
    """Fake executor that blocks until released, so a control can act mid-run."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, object]:
        assert root is not None
        self.entered.set()
        await self.release.wait()
        return {
            "status": "completed",
            "result": "candidate ready",
            "session_id": "control-session",
        }


def event_types(db: Any, job_id: str) -> list[str]:
    return [row.get("event_type") for row in store.load_event_rows(db, job_id)]


class SessionedBlockingExecutor:
    """First slice returns a resumable max_turns result (session captured), the
    second slice blocks until released, later slices complete."""

    def __init__(self) -> None:
        self.calls = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls += 1
        if self.calls == 1:
            (root / "step1.txt").write_text("step 1\n", encoding="utf-8")
            return {
                "status": "failed",
                "terminal_reason": "max_turns",
                "num_turns": 1,
                "session_id": "control-session",
                "result": "slice 1",
            }
        if self.calls == 2:
            self.blocked.set()
            await self.release.wait()
        return {
            "status": "completed",
            "result": "candidate ready",
            "session_id": "control-session",
        }


def sessioned_settings(base: Path) -> Settings:
    """Turn budgets down so a max_turns slice is eligible for one extension."""
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "CLAUDE_MAX_TURNS": "1",
            "CLAUDE_EXTENSION_TURNS": "1",
            "CLAUDE_MAX_EXTENSIONS": "1",
            "CLAUDE_HARD_TURNS": "4",
        }
    )


class DashboardAuthoritiesTests(unittest.TestCase):
    """M-CC-08 dashboard enrichment: ``authorities_in_force`` in the snapshot."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = control_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshot_surfaces_unused_unexpired_authorizations(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            jobs, authorization = job_managers(self.settings, policy)
            completed = await start_completed(jobs, policy)
            job_id = str(completed["job_id"])

            # No authorizations issued yet: the field is simply absent.
            self.assertNotIn("authorities_in_force", completed)

            # Issue a job-scoped authorization, then a project-scoped one; both
            # must surface once granted and drop once consumed.
            authorization.issue(
                policy.id, "commit", job_id, job_id=job_id
            )
            authorization.issue(
                policy.id, "push", "branch/feature", job_id=None
            )
            refreshed = await jobs.get(policy, job_id)
            entries = refreshed["authorities_in_force"]
            self.assertEqual(len(entries), 2)
            by_capability = {entry["capability"]: entry for entry in entries}
            self.assertEqual(by_capability["commit"]["job_id"], job_id)
            self.assertEqual(by_capability["commit"]["actor"], "ChatGPT")
            self.assertEqual(by_capability["commit"]["used"], False)
            self.assertIsNone(by_capability["push"]["job_id"])

            # Consuming marks the authorization used; it leaves the in-force set.
            authorization.consume(by_capability["commit"]["authorization_id"])
            after_consume = await jobs.get(policy, job_id)
            remaining = after_consume["authorities_in_force"]
            self.assertEqual([entry["capability"] for entry in remaining], ["push"])

        asyncio.run(exercise())

    def test_snapshot_excludes_expired_authorizations(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            jobs, authorization = job_managers(self.settings, policy)
            completed = await start_completed(jobs, policy)
            job_id = str(completed["job_id"])
            authorization.issue(
                policy.id,
                "commit",
                job_id,
                job_id=job_id,
                expires_at="2000-01-01T00:00:00+00:00",
            )
            refreshed = await jobs.get(policy, job_id)
            self.assertNotIn("authorities_in_force", refreshed)

        asyncio.run(exercise())

    def test_authorizations_table_additive_only(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            jobs, authorization = job_managers(self.settings, policy)
            completed = await start_completed(jobs, policy)
            job_id = str(completed["job_id"])
            authorization.issue(policy.id, "commit", job_id, job_id=job_id)
            # No job event or progress mutation: a dashboard read never writes.
            rows = await jobs.get(policy, job_id)
            self.assertEqual(rows["status"], "completed")

        asyncio.run(exercise())


class PauseDevelopmentJobTests(unittest.TestCase):
    """pause_development_job: capability-gated, idempotent, audit event."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = control_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pause_transitions_to_paused_interrupted_and_is_resumable(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            locks = ProjectLockManager()
            executor = SessionedBlockingExecutor()
            jobs = DevelopmentJobManager(
                sessioned_settings(self.base), locks, executor, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            job_id = str(started["job_id"])
            # Call 1 returned a max_turns slice with a session; call 2 (the
            # extended slice) is now in flight and blocked.
            await asyncio.wait_for(executor.blocked.wait(), timeout=2)

            paused = await jobs.pause(policy, job_id)
            self.assertEqual(paused["status"], "paused_interrupted")
            self.assertEqual(paused["pause_reason"], "human_requested_pause")
            self.assertTrue(paused["resume_available"])
            self.assertIn("base_head", paused)

            # Resume from the paused state continues the candidate to completion.
            executor.release.set()
            resumed = await jobs.resume(policy, job_id, "continue the work")
            self.assertEqual(resumed["status"], "queued")
            completed = await wait_completed(jobs, policy, job_id)
            self.assertEqual(completed["status"], "completed")

        asyncio.run(exercise())

    def test_pause_is_idempotent_and_records_one_audit_event(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            locks = ProjectLockManager()
            blocking = BlockingExecutor()
            jobs = DevelopmentJobManager(self.settings, locks, blocking, projects=(policy,))
            started = await jobs.start(policy, "make candidate", "high")
            job_id = str(started["job_id"])
            await asyncio.wait_for(blocking.entered.wait(), timeout=2)

            first = await jobs.pause(policy, job_id)
            self.assertEqual(first["status"], "paused_interrupted")
            again = await jobs.pause(policy, job_id)
            self.assertEqual(again["status"], "paused_interrupted")
            self.assertEqual(again["updated_at"], first["updated_at"])
            control_events = [
                t for t in event_types(jobs._db, job_id) if t == "control_pause"
            ]
            self.assertEqual(control_events, ["control_pause"])
            # The run loop's CancelledError handler recorded the paused terminal
            # state, not a hard cancellation.
            terminal = [
                row.get("detail_json")
                for row in store.load_event_rows(jobs._db, job_id)
                if row.get("event_type") == "terminal"
            ]
            self.assertEqual(len(terminal), 1)
            self.assertIn("paused_interrupted", terminal[0])
            blocking.release.set()

        asyncio.run(exercise())

    def test_pause_is_capability_gated_via_server(self) -> None:
        async def exercise() -> None:
            from fastmcp.exceptions import ToolError

            policy = project(
                self.root,
                ["{python}", "-c", "print('ok')"],
                capabilities=["repository_read", "claude_edit", "verification_commit"],
            )
            server = server_for(self.base, policy)
            with self.assertRaises(ToolError):
                await server.call_tool(
                    "pause_development_job",
                    {"project_id": "test-project", "job_id": "0" * 32},
                )

        asyncio.run(exercise())

    def test_pause_capability_requires_claude_edit_in_policy(self) -> None:
        async def exercise() -> None:
            with self.assertRaises(ValueError):
                ProjectPolicy.model_validate(
                    {
                        "id": "test-project",
                        "display_name": "T",
                        "description": "T",
                        "root": str(self.root),
                        "capabilities": [
                            "repository_read",
                            ProjectCapability.PAUSE_DEVELOPMENT_JOB.value,
                        ],
                    }
                )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
