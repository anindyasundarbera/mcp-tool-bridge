import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobError, DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager, ProjectRegistry
from local_dev_mcp.server import create_server
from local_dev_mcp.store import StateStoreError
from local_dev_mcp.verification import VerificationCommitManager, VerificationError

from tests.support import initialize_git_repository
from tests.test_jobs import (
    BlockingExecutor,
    ImmediateExecutor,
    policy as job_policy,
    settings as job_settings,
    wait_for_status,
)
from tests.test_verification import (
    EditingExecutor,
    project as verification_policy,
    wait_completed,
)


def make_project(root: Path, **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Persistence tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "markdown_write",
            "claude_edit",
            "verification_commit",
        ],
        "markdown_write_roots": ["docs"],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": ["{python}", "-c", "print('ok')"]}
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        # A committed src/ tree so verification projects with python_path=["src"]
        # resolve in the retained worktree.
        (self.root / "src").mkdir()
        (self.root / "src" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
        import subprocess

        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add source"], check=True
        )
        self.settings = job_settings(self.base)
        # jobs-only project (no verification capability) for the job tests.
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _db_path(self) -> Path:
        return self.settings.job_worktree_root / ".state" / "bridge.db"

    def test_completed_job_survives_restart_and_resume_reuses_session(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(a, self.project, job_id, {"completed"})
            self.assertTrue(completed["resume_available"])

            # Simulate a restart: a brand-new manager re-hydrates from the same
            # database (its own connection to the same bridge.db).
            executor_b = ImmediateExecutor()
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_b
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "completed")
            self.assertTrue(reloaded["resume_available"])

            resumed = await b.resume(self.project, job_id, "continue", "ultracode")
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["attempt"], 2)
            await wait_for_status(b, self.project, job_id, {"completed"})
            # Resume on B reused the session_id persisted by A's first run, and
            # used the effort from the resume call.
            self.assertEqual(executor_b.calls[0]["resume_session_id"], "bridge-session-1")
            self.assertEqual(executor_b.calls[0]["effort"], "ultracode")

        asyncio.run(exercise())

    def test_nonterminal_job_is_marked_failed_on_reload(self) -> None:
        async def exercise() -> None:
            executor_a = BlockingExecutor()
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_a
            )
            started = await a.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            await asyncio.wait_for(executor_a.entered.wait(), timeout=2)
            self.assertEqual(
                (await a.get(self.project, job_id))["status"], "running"
            )

            # Simulate a crash mid-flight: a fresh manager sees the persisted
            # running row and reconciles it to failed (the task/process is gone).
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "failed")
            self.assertIn("bridge restarted", str(reloaded["error"]))

            # Clean up A's still-running task so the loop can close cleanly.
            await a.cancel(self.project, job_id)

        asyncio.run(exercise())

    def test_unparseable_job_row_is_skipped_others_intact(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            started = await a.start(self.project, "first", "high")
            good_id = str(started["job_id"])
            await wait_for_status(a, self.project, good_id, {"completed"})

            # Inject a malformed row (unknown status) directly into the db.
            conn = sqlite3.connect(self._db_path())
            conn.execute(
                "INSERT INTO jobs (job_id, project_id, effort, status, attempt, "
                "created_at, updated_at, cancel_requested) VALUES "
                "('deadbeefdeadbeefdeadbeefdeadbeef', 'test-project', 'high', "
                "'bogus', 1, 't', 't', 0)"
            )
            conn.commit()
            conn.close()

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            # The valid job reloaded fine.
            self.assertEqual(
                (await b.get(self.project, good_id))["status"], "completed"
            )
            # The malformed row was skipped -> unknown job_id (fail-closed).
            with self.assertRaisesRegex(DevelopmentJobError, "unknown job_id"):
                await b.get(self.project, "deadbeefdeadbeefdeadbeefdeadbeef")

        asyncio.run(exercise())

    def test_unknown_schema_version_refuses_to_start(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            started = await a.start(self.project, "first", "high")
            await wait_for_status(a, self.project, str(started["job_id"]), {"completed"})

            conn = sqlite3.connect(self._db_path())
            conn.execute("UPDATE schema_meta SET value = '999' WHERE key = 'version'")
            conn.commit()
            conn.close()

            with self.assertRaisesRegex(StateStoreError, "newer than supported"):
                DevelopmentJobManager(
                    self.settings, ProjectLockManager(), ImmediateExecutor()
                )

        asyncio.run(exercise())

    def test_deleted_worktree_is_marked_failed_on_reload(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            worktree_path = (
                self.settings.job_worktree_root / self.project.id / job_id
            )
            self.assertTrue(worktree_path.is_dir())
            shutil.rmtree(worktree_path)

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "failed")
            self.assertIn("no longer exists", str(reloaded["error"]))
            self.assertFalse(reloaded["resume_available"])
            self.assertNotIn("base_head", reloaded)

        asyncio.run(exercise())

    def test_verification_record_survives_restart_and_remains_single_use(self) -> None:
        async def exercise() -> None:
            command = ["{python}", "-c", "print('ok')"]
            project = verification_policy(self.root, command)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text(
                    "candidate\n", encoding="utf-8"
                )

            locks_a = ProjectLockManager()
            jobs_a = DevelopmentJobManager(
                self.settings, locks_a, EditingExecutor(edit), projects=(project,)
            )
            ver_a = VerificationCommitManager(
                self.settings, locks_a, jobs_a, projects=(project,)
            )
            started = await jobs_a.start(project, "candidate", "high")
            job_id = str(started["job_id"])
            completed = await wait_completed(jobs_a, project, job_id)
            self.assertEqual(completed["status"], "completed")
            verified = await ver_a.verify(project, job_id, completed["base_head"])
            self.assertEqual(verified["status"], "passed")
            verification_id = verified["verification_id"]
            base_head = verified["base_head"]
            diff_fp = verified["diff_fingerprint"]
            paths = list(verified["paths"])

            # Simulate a restart: fresh managers re-hydrate both the job and
            # the verification record from the same database.
            locks_b = ProjectLockManager()
            jobs_b = DevelopmentJobManager(
                self.settings, locks_b, EditingExecutor(edit), projects=(project,)
            )
            ver_b = VerificationCommitManager(
                self.settings, locks_b, jobs_b, projects=(project,)
            )

            # The verification_id persisted by A is still valid on B.
            committed = await ver_b.commit(
                project,
                job_id,
                verification_id,
                base_head,
                diff_fp,
                paths,
                "MCPB restart commit",
            )
            self.assertEqual(committed["status"], "committed")
            self.assertTrue(committed["detached"])

            # Single-use: a second commit with the same id is rejected.
            with self.assertRaisesRegex(VerificationError, "already been used"):
                await ver_b.commit(
                    project,
                    job_id,
                    verification_id,
                    base_head,
                    diff_fp,
                    paths,
                    "Replay",
                )

            # The committed job is no longer resumable.
            final = await jobs_b.get(project, job_id)
            self.assertFalse(final["resume_available"])
            self.assertEqual(final["commit_sha"], committed["commit_sha"])
            with self.assertRaisesRegex(
                DevelopmentJobError, "a committed development job cannot resume"
            ):
                await jobs_b.resume(project, job_id, "again", "high")

        asyncio.run(exercise())

    def test_attempt_created_at_effort_round_trip_after_resume(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            first = await wait_for_status(a, self.project, job_id, {"completed"})
            created_at = first["created_at"]
            resumed = await a.resume(self.project, job_id, "continue", "ultracode")
            self.assertEqual(resumed["attempt"], 2)
            await wait_for_status(a, self.project, job_id, {"completed"})

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["attempt"], 2)
            self.assertEqual(reloaded["effort"], "ultracode")
            self.assertEqual(reloaded["created_at"], created_at)

        asyncio.run(exercise())

    def test_durable_state_adds_no_new_mcp_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            initialize_git_repository(root)
            (root / "docs").mkdir()
            project = make_project(root)
            server = create_server(
                Settings.from_env(
                    {
                        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                        "JOB_WORKTREE_ROOT": str(base / "jobs"),
                    }
                ),
                ProjectRegistry({"test-project": project}),
            )
            tools = {tool.name for tool in asyncio.run(server.list_tools())}

        # Persistence is internal; it must not add any MCP tool.
        self.assertEqual(len(tools), 15)
        for leaked in ("persist_job", "list_development_jobs", "reload_jobs"):
            self.assertNotIn(leaked, tools)
        # resume still takes job_id, never session_id.
        self.assertIn("resume_development_job", tools)

    def test_state_db_uses_wal_and_full_synchronous(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()
            )
            journal = a._db.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = a._db.execute("PRAGMA synchronous").fetchone()[0]
            self.assertEqual(str(journal).lower(), "wal")
            self.assertEqual(int(synchronous), 2)  # FULL

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()