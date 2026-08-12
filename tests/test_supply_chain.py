"""M-CC-11: severity-gated supply-chain gate.

These tests verify that a project's opt-in ``supply_chain_scans`` run at commit
time with fixed argv, reject the commit on a non-zero exit (fail-closed), leave
the repository unchanged on rejection, and append an audit row per scan. They
use fake scanners (``{python} -c ...``) so no real syft/grype/pip-audit binary
or network is required.
"""

import asyncio
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.store import load_supply_chain_scans, open_state_db
from local_dev_mcp.verification import VerificationCommitManager, VerificationError

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, project as verification_project
from tests.test_jobs import wait_for_status


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "5",
            "MAX_VERIFICATION_OUTPUT_BYTES": "64",
        }
    )


def scan_policy(
    root: Path,
    command: list[str],
    scans: list[dict[str, Any]],
    **overrides: object,
) -> ProjectPolicy:
    return verification_project(
        root,
        command,
        supply_chain_scans=scans,
        **overrides,
    )


class SupplyChainGateTests(unittest.TestCase):
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
        self.settings = settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _db_path(self) -> Path:
        return self.settings.job_worktree_root / ".state" / "bridge.db"

    async def prepare(
        self,
        policy: ProjectPolicy,
        edit: Any,
    ) -> tuple[DevelopmentJobManager, VerificationCommitManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(edit),  # type: ignore[arg-type]
            projects=(policy,),
        )
        manager = VerificationCommitManager(
            self.settings,
            locks,
            jobs,
            projects=(policy,),
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_for_status(
            jobs, policy, str(started["job_id"]), {"completed"}
        )
        return jobs, manager, completed

    async def verify_and_commit(
        self,
        policy: ProjectPolicy,
        manager: VerificationCommitManager,
        completed: dict[str, Any],
    ) -> dict[str, Any]:
        verified = await manager.verify(
            policy, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        return await manager.commit(
            policy,
            str(completed["job_id"]),
            str(verified["verification_id"]),
            str(verified["base_head"]),
            str(verified["diff_fingerprint"]),
            list(verified["paths"]),
            "MCPB supply-chain candidate",
        )

    def test_passing_scanner_allows_commit(self) -> None:
        async def exercise() -> None:
            command = ["{python}", "-c", "import sys; sys.exit(0)"]
            scans = [
                {
                    "id": "grype",
                    "argv": ["{python}", "-c", "import sys; sys.exit(0)"],
                }
            ]
            policy = scan_policy(self.root, command, scans)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            committed = await self.verify_and_commit(policy, manager, completed)
            self.assertEqual(committed["status"], "committed")

            rows = load_supply_chain_scans(manager._db, str(completed["job_id"]))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["scan_command"], "grype")
            self.assertEqual(rows[0]["passed"], 1)
            self.assertEqual(rows[0]["exit_code"], 0)

        asyncio.run(exercise())

    def test_failing_scanner_rejects_commit_and_leaves_repo_unchanged(self) -> None:
        async def exercise() -> None:
            command = ["{python}", "-c", "import sys; sys.exit(0)"]
            scans = [
                {
                    "id": "grype",
                    "argv": ["{python}", "-c", "import sys; sys.exit(2)"],
                }
            ]
            policy = scan_policy(self.root, command, scans)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            source_head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            jobs, manager, completed = await self.prepare(policy, edit)
            with self.assertRaisesRegex(VerificationError, "supply-chain gate rejected"):
                await self.verify_and_commit(policy, manager, completed)

            # The source checkout is untouched and the candidate worktree is
            # still intact (no commit was created).
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                source_head,
            )
            rows = load_supply_chain_scans(manager._db, str(completed["job_id"]))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["passed"], 0)
            self.assertEqual(rows[0]["exit_code"], 2)

        asyncio.run(exercise())

    def test_opted_out_project_runs_no_scans(self) -> None:
        async def exercise() -> None:
            command = ["{python}", "-c", "import sys; sys.exit(0)"]
            policy = verification_project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            committed = await self.verify_and_commit(policy, manager, completed)
            self.assertEqual(committed["status"], "committed")
            rows = load_supply_chain_scans(manager._db, str(completed["job_id"]))
            self.assertEqual(rows, [])

        asyncio.run(exercise())

    def test_missing_scanner_fails_closed(self) -> None:
        async def exercise() -> None:
            command = ["{python}", "-c", "import sys; sys.exit(0)"]
            scans = [
                {
                    "id": "grype",
                    "argv": ["definitely-not-a-real-scanner-binary-xyz", "--fail-on=high"],
                }
            ]
            policy = scan_policy(self.root, command, scans)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            with self.assertRaisesRegex(VerificationError, "supply-chain gate rejected"):
                await self.verify_and_commit(policy, manager, completed)

        asyncio.run(exercise())

    def test_supply_chain_table_is_additive(self) -> None:
        db_path = self._db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, project_id, effort, status, attempt, "
                "created_at, updated_at, cancel_requested) VALUES "
                "('legacyjob', 'test-project', 'high', 'completed', 1, 't', 't', 0)"
            )
            connection.commit()
            connection.execute("DROP TABLE supply_chain_scans")
            connection.commit()
        finally:
            connection.close()

        connection = open_state_db(db_path)
        try:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                ("supply_chain_scans",),
            ).fetchone()
            self.assertIsNotNone(row, "supply_chain_scans should be recreated on open")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id='legacyjob'"
            ).fetchone()
            self.assertEqual(row["status"], "completed")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
