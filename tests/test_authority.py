"""M-EV-05: authority-behind-every-governance-instruction.

These tests verify that governance-changing events (status / extension /
pause / terminal) are stamped with an authority record (actor, authority
source, scope, classification) persisted to the additive ``authority_records``
sidecar table, and surfaced in the job snapshot. They use fake executors (no
real Claude, no network), mirroring ``tests/test_checkpoints.py``.
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.store import (
    load_authority_records,
    load_source_classifications,
    open_state_db,
)

from tests.support import initialize_git_repository
from tests.test_jobs import (
    ImmediateExecutor,
    policy as job_policy,
    settings as job_settings,
    wait_for_status,
)


def authority_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9102",
    }
    payload.update(env)
    return Settings.from_env(payload)


class AuthorityRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = authority_settings(self.base)
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _db_path(self) -> Path:
        return self.settings.job_worktree_root / ".state" / "bridge.db"

    def test_governance_events_are_stamped_with_authority(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )

            # The snapshot surfaces the authority records behind the events.
            records = completed.get("authority_records")
            self.assertIsNotNone(records)
            self.assertGreaterEqual(len(records), 1)

            # The durable sidecar table holds the same records.
            rows = load_authority_records(manager._db, job_id)
            self.assertGreaterEqual(len(rows), 1)
            by_scope = {row["scope"]: row for row in rows}
            self.assertIn("job_creation", by_scope)
            self.assertEqual(by_scope["job_creation"]["actor"], "ChatGPT")
            self.assertEqual(
                by_scope["job_creation"]["authority_source"], "chatgpt_confirmation"
            )
            self.assertEqual(by_scope["job_creation"]["classification"], "canonical")

            # Every authority record has a non-empty authority_id.
            for row in rows:
                self.assertTrue(row["authority_id"])
                self.assertTrue(row["classification"])

        asyncio.run(exercise())

    def test_authority_records_survive_reload(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            await wait_for_status(manager, self.project, job_id, {"completed"})

            # A fresh manager re-hydrates the authority records from the same db.
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertGreaterEqual(len(reloaded.get("authority_records") or []), 1)

        asyncio.run(exercise())

    def test_source_classifications_are_seeded(self) -> None:
        db_path = self._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        try:
            classifications = load_source_classifications(connection)
            names = {row["classification"] for row in classifications}
            for expected in (
                "canonical",
                "historical",
                "asserted",
                "observed",
                "tested",
                "inferred",
                "superseded",
            ):
                self.assertIn(expected, names)
        finally:
            connection.close()

    def test_authority_tables_are_additive(self) -> None:
        connection, db_path = self._fresh_db()
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, project_id, effort, status, attempt, "
                "created_at, updated_at, cancel_requested) VALUES "
                "('legacyjob', 'test-project', 'high', 'completed', 1, 't', 't', 0)"
            )
            connection.commit()
            # Simulate a pre-M-EV-05 database: drop the authority tables.
            connection.execute("DROP TABLE authority_records")
            connection.execute("DROP TABLE source_classifications")
            connection.commit()
        finally:
            connection.close()

        # Re-opening recreates them additively; the legacy job row survives.
        connection = open_state_db(db_path)
        try:
            for table in ("authority_records", "source_classifications"):
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                self.assertIsNotNone(row, f"{table} should be recreated on open")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id='legacyjob'"
            ).fetchone()
            self.assertEqual(row["status"], "completed")
        finally:
            connection.close()

    def _fresh_db(self) -> tuple[sqlite3.Connection, Path]:
        db_path = self._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        return connection, db_path


if __name__ == "__main__":
    unittest.main()
