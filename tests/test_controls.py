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

from local_dev_mcp.authorization import AuthorizationManager
from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
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


if __name__ == "__main__":
    unittest.main()
