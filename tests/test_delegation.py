"""M-GV-06: Sub-agent delegation + parent/child task trees.

Tests exercise bounded parent->child delegation with fake executors (no real
Claude, no network): a child is linked to its parent, the delegation is
recorded in the ``delegations`` ledger, the terminal report surfaces the tree,
and over-delegation is rejected.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobError, DevelopmentJobManager
from local_dev_mcp.models import Effort, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager

from tests.support import initialize_git_repository
from tests.test_jobs import ImmediateExecutor, policy, settings, wait_for_status


class DelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = settings(self.base)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self, executor: Any) -> DevelopmentJobManager:
        return DevelopmentJobManager(
            self.settings,
            ProjectLockManager(),
            executor,  # type: ignore[arg-type]
            projects=(self.project,),
        )

    def test_child_is_linked_to_parent_and_tree_surfaces(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            child = await manager.delegate(
                self.project,
                parent_id,
                "child work",
                "high",
                mandate={"inputs": ["candidate.txt"], "outputs": ["candidate.txt"]},
            )
            child_id = str(child["job_id"])
            self.assertEqual(child["parent_job_id"], parent_id)
            self.assertEqual(
                child["delegation_mandate"]["outputs"], ["candidate.txt"]
            )
            await wait_for_status(manager, self.project, child_id, {"completed"})

            # The parent's snapshot surfaces the child in its delegation tree.
            parent_snapshot = await manager.get(self.project, parent_id)
            self.assertEqual(parent_snapshot["children"][0]["child_job_id"], child_id)
            self.assertEqual(
                parent_snapshot["children"][0]["mandate"]["outputs"],
                ["candidate.txt"],
            )

        asyncio.run(exercise())

    def test_over_delegation_is_rejected(self) -> None:
        async def exercise() -> None:
            # Bound the parent to 1 child.
            self.settings = Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                    "CANDIDATE_MCP_PORT": "9101",
                    "MAX_DELEGATIONS_PER_JOB": "1",
                }
            )
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            await manager.delegate(
                self.project, parent_id, "child 1", "high",
                mandate={"outputs": ["candidate.txt"]},
            )
            with self.assertRaisesRegex(DevelopmentJobError, "delegation limit"):
                await manager.delegate(
                    self.project, parent_id, "child 2", "high",
                    mandate={"outputs": ["candidate.txt"]},
                )

        asyncio.run(exercise())

    def test_delegate_requires_real_parent(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            with self.assertRaisesRegex(DevelopmentJobError, "unknown job_id"):
                await manager.delegate(
                    self.project, "0" * 32, "child", "high",
                    mandate={"outputs": ["candidate.txt"]},
                )

        asyncio.run(exercise())

    def test_child_out_of_scope_is_rejected(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            # The executor writes candidate.txt, which is outside the mandate's
            # declared outputs (docs/feature.md), so the child must fail.
            child = await manager.delegate(
                self.project,
                parent_id,
                "child work",
                "high",
                mandate={"outputs": ["docs/feature.md"]},
            )
            child_id = str(child["job_id"])
            snapshot = await wait_for_status(
                manager, self.project, child_id, {"failed"}
            )
            self.assertIn("outside its mandate", snapshot["error"])

        asyncio.run(exercise())

    def test_child_cannot_delegate_further(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            child = await manager.delegate(
                self.project, parent_id, "child work", "high",
                mandate={"outputs": ["candidate.txt"]},
            )
            child_id = str(child["job_id"])
            await wait_for_status(manager, self.project, child_id, {"completed"})

            with self.assertRaisesRegex(DevelopmentJobError, "cannot delegate further"):
                await manager.delegate(
                    self.project, child_id, "grandchild", "high",
                    mandate={"outputs": ["candidate.txt"]},
                )

        asyncio.run(exercise())

    def test_invalid_mandate_is_rejected(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            with self.assertRaisesRegex(DevelopmentJobError, "unsupported keys"):
                await manager.delegate(
                    self.project,
                    parent_id,
                    "child",
                    "high",
                    mandate={"evil": ["x"]},
                )

        asyncio.run(exercise())

    def test_mandate_is_required(self) -> None:
        async def exercise() -> None:
            manager = self._manager(ImmediateExecutor())
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            with self.assertRaisesRegex(DevelopmentJobError, "mandate is required"):
                await manager.delegate(
                    self.project, parent_id, "child", "high", mandate=None
                )

        asyncio.run(exercise())

    def test_legacy_child_with_null_mandate_fails_closed(self) -> None:
        """A child row with parent_job_id but a NULL mandate is not promoted."""
        class GatedExecutor:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.entered = asyncio.Event()
                self.calls = 0

            async def execute(self, project, specification, effort, *, root=None,
                              resume_session_id=None, max_turns=None):
                assert root is not None
                self.calls += 1
                (root / "candidate.txt").write_text("x\n", encoding="utf-8")
                if self.calls == 1:
                    # Parent job: complete immediately.
                    return {"status": "completed", "result": "parent",
                            "session_id": "p"}
                # Child job: block until released.
                self.entered.set()
                await self.release.wait()
                return {"status": "completed", "result": "done",
                        "session_id": "s"}

        async def exercise() -> None:
            executor = GatedExecutor()
            manager = self._manager(executor)
            parent = await manager.start(self.project, "parent work", "high")
            parent_id = str(parent["job_id"])
            await wait_for_status(manager, self.project, parent_id, {"completed"})

            child = await manager.delegate(
                self.project, parent_id, "child work", "high",
                mandate={"outputs": ["candidate.txt"]},
            )
            child_id = str(child["job_id"])
            await asyncio.wait_for(executor.entered.wait(), timeout=2)
            # Simulate a legacy row: clear the in-memory mandate so the
            # completion-time enforcement sees a NULL mandate.
            job = manager._jobs[child_id]
            job.delegation_mandate = None
            executor.release.set()

            snapshot = await wait_for_status(
                manager, self.project, child_id, {"failed"}
            )
            self.assertIn("no mandate", snapshot["error"])

        asyncio.run(exercise())

    def test_existing_job_progress_table_upgrades_in_place(self) -> None:
        """A pre-M-GV-06 job_progress table gains the delegation columns on open."""
        import sqlite3

        from local_dev_mcp.store import open_state_db

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.db"
            # Create a DB with the OLD job_progress shape (no delegation columns).
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE job_progress (
                    job_id TEXT PRIMARY KEY,
                    pause_reason TEXT,
                    turns_used INTEGER NOT NULL,
                    tokens_used INTEGER NOT NULL,
                    extensions_granted INTEGER NOT NULL,
                    no_progress_turns INTEGER NOT NULL,
                    tool_failures INTEGER NOT NULL,
                    soft_warned INTEGER NOT NULL,
                    milestones_json TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.commit()
            connection.close()

            # Re-opening with the current code adds the delegation columns.
            connection = open_state_db(db_path)
            try:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(job_progress)"
                    ).fetchall()
                }
                self.assertIn("parent_job_id", columns)
                self.assertIn("delegation_mandate_json", columns)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
