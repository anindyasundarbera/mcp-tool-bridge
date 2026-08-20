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
from local_dev_mcp.store import StateStoreError, open_state_db
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
        self.assertEqual(len(tools), 31)
        for leaked in ("persist_job", "reload_jobs"):
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

    def _fresh_db_path(self) -> Path:
        db_path = self.base / "state" / "bridge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    def test_review_row_round_trips_through_persistence(self) -> None:
        import json as _json

        from local_dev_mcp.store import (
            load_review_rows,
            open_state_db,
            upsert_review_row,
        )

        db_path = self._fresh_db_path()
        connection = open_state_db(db_path)
        try:
            row = {
                "review_id": "rev-1",
                "project_id": "test-project",
                "job_id": "job-1",
                "base_head": "abc123",
                "diff_fingerprint": "sha256:deadbeef",
                "paths_json": _json.dumps(["src/a.py", "src/b.py"]),
                "reviewed_at": "2026-08-06T00:00:00Z",
                "reviewer_id": "claude-opus",
                "verdict_json": _json.dumps({"approved": True, "notes": "ok"}),
                "used": 0,
            }
            upsert_review_row(connection, row)

            reloaded = load_review_rows(connection)
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0], row)
            # Every field survives, including ``used``.
            self.assertEqual(reloaded[0]["review_id"], "rev-1")
            self.assertEqual(reloaded[0]["used"], 0)
            self.assertEqual(reloaded[0]["verdict_json"], row["verdict_json"])
            self.assertEqual(reloaded[0]["reviewer_id"], "claude-opus")

            # Upserting again with new values updates the single row in place.
            row["used"] = 1
            row["reviewer_id"] = "claude-sonnet"
            upsert_review_row(connection, row)
            reloaded = load_review_rows(connection)
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0]["used"], 1)
            self.assertEqual(reloaded[0]["reviewer_id"], "claude-sonnet")
        finally:
            connection.close()

    def test_review_id_exists_reports_presence(self) -> None:
        from local_dev_mcp.store import open_state_db, review_id_exists, upsert_review_row

        db_path = self._fresh_db_path()
        connection = open_state_db(db_path)
        try:
            row = {
                "review_id": "rev-exists",
                "project_id": "test-project",
                "job_id": "job-1",
                "base_head": "abc123",
                "diff_fingerprint": "sha256:deadbeef",
                "paths_json": "[]",
                "reviewed_at": "2026-08-06T00:00:00Z",
                "reviewer_id": None,
                "verdict_json": "{}",
                "used": 0,
            }
            self.assertFalse(review_id_exists(connection, "rev-exists"))
            self.assertFalse(review_id_exists(connection, "missing"))
            upsert_review_row(connection, row)
            self.assertTrue(review_id_exists(connection, "rev-exists"))
            self.assertFalse(review_id_exists(connection, "missing"))
        finally:
            connection.close()

    def test_publication_row_round_trips_and_is_unique_per_job(self) -> None:
        from local_dev_mcp.store import (
            load_publication_rows,
            open_state_db,
            publication_for,
            upsert_publication_row,
        )

        db_path = self._fresh_db_path()
        connection = open_state_db(db_path)
        try:
            row = {
                "project_id": "test-project",
                "job_id": "job-1",
                "commit_sha": "c0ffee",
                "branch_name": "agent/cks-002b",
                "remote_url": "git@example:repo.git",
                "published_at": "2026-08-07T00:00:00Z",
            }
            upsert_publication_row(connection, row)
            reloaded = load_publication_rows(connection)
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0], row)
            self.assertEqual(
                publication_for(connection, "test-project", "job-1"), row
            )
            self.assertIsNone(
                publication_for(connection, "test-project", "job-2")
            )
            # Upserting the same (project, job) with a new branch replaces the row.
            row["branch_name"] = "agent/cks-002c"
            upsert_publication_row(connection, row)
            reloaded = load_publication_rows(connection)
            self.assertEqual(len(reloaded), 1)
            self.assertEqual(reloaded[0]["branch_name"], "agent/cks-002c")
        finally:
            connection.close()

    def test_used_verification_record_matches_exact_evidence_only(self) -> None:
        import json as _json

        from local_dev_mcp.store import (
            open_state_db,
            upsert_record_row,
            used_verification_record,
        )

        db_path = self._fresh_db_path()
        connection = open_state_db(db_path)
        try:
            base = "b" * 40
            fingerprint = "f" * 64
            connection.execute(
                "INSERT INTO verifications (verification_id, project_id, job_id, "
                "base_head, diff_fingerprint, paths_json, verified_at, used, "
                "commands_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "ver-1", "test-project", "job-1", base, fingerprint,
                    _json.dumps(["docs/a.md"]), "2026-08-07T00:00:00Z", 1, "[]",
                ),
            )
            connection.commit()
            self.assertIsNone(
                used_verification_record(
                    connection, "test-project", "job-1", base, "0" * 64
                )
            )
            self.assertIsNone(
                used_verification_record(
                    connection, "test-project", "job-1", "0" * 40, fingerprint
                )
            )
            matched = used_verification_record(
                connection, "test-project", "job-1", base, fingerprint
            )
            self.assertIsNotNone(matched)
            self.assertEqual(matched["verification_id"], "ver-1")
            self.assertEqual(matched["used"], 1)
        finally:
            connection.close()

    def test_stale_schema_version_is_advanced_on_open(self) -> None:
        from local_dev_mcp import store

        db_path = self._fresh_db_path()
        # First open creates the schema at SCHEMA_VERSION (1).
        connection = open_state_db(db_path)
        connection.close()
        # Simulate a DB written by an older revision: pin the version below
        # SCHEMA_VERSION and re-open. open_state_db must advance it.
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE schema_meta SET value = '0' WHERE key = 'version'")
        conn.commit()
        conn.close()

        connection = open_state_db(db_path)
        try:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            self.assertEqual(int(row["value"]), store.SCHEMA_VERSION)
        finally:
            connection.close()

    def test_newer_schema_version_still_refuses_to_open(self) -> None:
        from local_dev_mcp import store

        db_path = self._fresh_db_path()
        connection = open_state_db(db_path)
        connection.close()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'",
            (str(store.SCHEMA_VERSION + 1),),
        )
        conn.commit()
        conn.close()

        with self.assertRaisesRegex(StateStoreError, "newer than supported"):
            open_state_db(db_path)


if __name__ == "__main__":
    unittest.main()