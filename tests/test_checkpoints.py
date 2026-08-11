"""Continuity release: durable checkpoints, paused states, soft/multi-dimensional
budgets, progress-gated extensions, and the structured terminal report.

These tests exercise the new job-lifecycle behavior with fake executors (no
real Claude, no network), mirroring the patterns in ``tests/test_jobs.py`` and
``tests/test_persistence.py``.
"""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager, JobStatus
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.store import (
    checkpoint_count,
    insert_checkpoint_row,
    load_checkpoint_rows,
    open_state_db,
)

from tests.support import initialize_git_repository
from tests.test_jobs import (
    BlockingExecutor,
    ImmediateExecutor,
    policy as job_policy,
    settings as job_settings,
    wait_for_status,
)


def continuity_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9101",
    }
    payload.update(env)
    return Settings.from_env(payload)


class MaxTurnsProgressExecutor:
    """Each call writes a new file (changed set grows) and returns a max_turns
    terminal reason with a resumable session and a fixed turn count.

    Models a drafter that keeps advancing but keeps hitting its per-slice turn
    limit — the condition that makes a progress-gated extension eligible.
    """

    def __init__(self, num_turns: int = 30) -> None:
        self.calls: list[dict[str, Any]] = []
        self.num_turns = num_turns

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
        n = len(self.calls) + 1
        self.calls.append(
            {
                "root": root,
                "text": specification,
                "resume_session_id": resume_session_id,
                "max_turns": max_turns,
            }
        )
        (root / f"step{n}.txt").write_text(f"step {n}\n", encoding="utf-8")
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-progress",
            "result": f"slice {n}",
        }


class StalledExecutor:
    """Writes nothing and blocks briefly so the progress sampler can observe a
    stalled (empty) changed set, then returns a max_turns slice."""

    def __init__(self, delay: float = 0.3, num_turns: int = 5) -> None:
        self.delay = delay
        self.num_turns = num_turns
        self.calls = 0

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
        await asyncio.sleep(self.delay)
        return {
            "status": "failed",
            "terminal_reason": "max_turns",
            "num_turns": self.num_turns,
            "session_id": "bridge-session-stalled",
            "result": "stalled",
        }


class UsageExecutor:
    """Returns a completed slice with token/turn usage so the budget counters
    can be exercised deterministically."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / "candidate.txt").write_text("done\n", encoding="utf-8")
        return {
            "status": "completed",
            "session_id": "bridge-session-usage",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 25},
            "num_turns": 3,
            "result": "done",
        }


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = continuity_settings(self.base)
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _db_path(self) -> Path:
        return self.settings.job_worktree_root / ".state" / "bridge.db"

    def _flip_status(self, job_id: str, status: str) -> None:
        conn = sqlite3.connect(self._db_path())
        conn.execute("UPDATE jobs SET status = ? WHERE job_id = ?", (status, job_id))
        conn.commit()
        conn.close()

    def test_checkpoints_appended_and_rehydrated(self) -> None:
        async def exercise() -> None:
            executor = MaxTurnsProgressExecutor(num_turns=40)
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            self.assertGreaterEqual(paused["checkpoint_count"], 2)
            # At least one extension checkpoint was recorded.
            rows = load_checkpoint_rows(manager._db, job_id)
            kinds = {row["kind"] for row in rows}
            self.assertIn("boundary", kinds)
            self.assertIn("extension", kinds)

            # A fresh manager re-hydrates the checkpoints from the same db.
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "paused_budget_exhausted")
            self.assertEqual(reloaded["checkpoint_count"], paused["checkpoint_count"])

        asyncio.run(exercise())

    def test_progress_grants_extension_and_then_hard_cap_pauses(self) -> None:
        async def exercise() -> None:
            settings = continuity_settings(
                self.base, CLAUDE_MAX_EXTENSIONS="2", CLAUDE_HARD_TURNS="100"
            )
            executor = MaxTurnsProgressExecutor(num_turns=40)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            # Two extensions were granted (3 executor slices), cumulative turns
            # crossed the hard cap, and each extension slice resumed the same
            # Claude session rather than restarting.
            self.assertEqual(len(executor.calls), 3)
            self.assertEqual(paused["budget"]["extensions_granted"], 2)
            self.assertGreaterEqual(paused["budget"]["turns_used"], 100)
            self.assertEqual(executor.calls[0]["max_turns"], 30)
            self.assertEqual(executor.calls[1]["max_turns"], 30)
            self.assertEqual(executor.calls[1]["resume_session_id"], "bridge-session-progress")
            self.assertEqual(executor.calls[2]["resume_session_id"], "bridge-session-progress")
            self.assertIn("hard turn cap", str(paused["pause_reason"]))

        asyncio.run(exercise())

    def test_no_progress_terminates_to_paused(self) -> None:
        async def exercise() -> None:
            settings = continuity_settings(
                self.base,
                CLAUDE_CHECKPOINT_INTERVAL_SECONDS="0.02",
                CLAUDE_MAX_NO_PROGRESS_TURNS="3",
                CLAUDE_HARD_TURNS="1000",
            )
            executor = StalledExecutor(delay=0.3, num_turns=5)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            paused = await wait_for_status(
                manager, self.project, job_id, {"paused_budget_exhausted"}
            )
            # The sampler observed an empty changed set across ticks; the slice
            # ended non-completed and the boundary paused on no-progress.
            self.assertEqual(paused["status"], "paused_budget_exhausted")
            self.assertIn("no progress", str(paused["pause_reason"]))
            self.assertGreaterEqual(paused["budget"]["no_progress_turns"], 3)

        asyncio.run(exercise())

    def test_crashed_job_with_checkpoints_becomes_paused_interrupted(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self.assertGreaterEqual(
                (await a.get(self.project, job_id))["checkpoint_count"], 1
            )

            # Simulate a crash mid-flight: flip the durable status back to
            # "running" while the checkpoint ledger is intact.
            self._flip_status(job_id, "running")

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "paused_interrupted")
            self.assertIn("bridge restarted", str(reloaded["pause_reason"]))
            # It retained a session + worktree, so it is resumable.
            self.assertTrue(reloaded["resume_available"])
            self.assertGreaterEqual(reloaded["checkpoint_count"], 1)

        asyncio.run(exercise())

    def test_crashed_job_without_checkpoints_still_failed(self) -> None:
        async def exercise() -> None:
            # Default 30s sampler interval means no sample fires before the
            # reload; a checkpoint-less crashed job must remain failed.
            executor_a = BlockingExecutor()
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_a  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            await asyncio.wait_for(executor_a.entered.wait(), timeout=2)

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            reloaded = await b.get(self.project, job_id)
            self.assertEqual(reloaded["status"], "failed")
            self.assertIn("bridge restarted", str(reloaded["error"]))

            await a.cancel(self.project, job_id)

        asyncio.run(exercise())

    def test_paused_job_resumes_from_last_checkpoint(self) -> None:
        async def exercise() -> None:
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self._flip_status(job_id, "running")

            executor_b = ImmediateExecutor()
            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor_b  # type: ignore[arg-type]
            )
            paused = await b.get(self.project, job_id)
            self.assertEqual(paused["status"], "paused_interrupted")

            resumed = await b.resume(self.project, job_id, "continue", "high")
            self.assertEqual(resumed["attempt"], 2)
            await wait_for_status(b, self.project, job_id, {"completed"})

            # The resume seeded the drafter prompt with the last checkpoint's
            # candidate state and resumed the bridge-created session.
            self.assertEqual(
                executor_b.calls[0]["resume_session_id"], "bridge-session-1"
            )
            self.assertIn("Resuming a previously paused job", executor_b.calls[0]["text"])
            self.assertIn("candidate.txt", executor_b.calls[0]["text"])

        asyncio.run(exercise())

    def test_non_resumable_terminal_states_reject_resume(self) -> None:
        async def exercise() -> None:
            # Quarantined provenance is terminal and not resumable. Inject it
            # directly and assert resume fails closed.
            a = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            started = await a.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(a, self.project, job_id, {"completed"})
            self._flip_status(job_id, JobStatus.QUARANTINED_PROVENANCE.value)

            b = DevelopmentJobManager(
                self.settings, ProjectLockManager(), ImmediateExecutor()  # type: ignore[arg-type]
            )
            quarantined = await b.get(self.project, job_id)
            self.assertEqual(quarantined["status"], "quarantined_provenance")
            self.assertFalse(quarantined["resume_available"])
            with self.assertRaises(Exception):
                await b.resume(self.project, job_id, "again", "high")

        asyncio.run(exercise())

    def test_terminal_report_contract_fields(self) -> None:
        async def exercise() -> None:
            executor = UsageExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            for key in (
                "prohibited_actions",
                "budget",
                "checkpoint_count",
                "milestones",
                "blockers",
                "candidate_fingerprint",
                "changed_path_count",
            ):
                self.assertIn(key, completed)
            self.assertIn("merge", completed["prohibited_actions"])
            self.assertIn("tag", completed["prohibited_actions"])
            self.assertIn("force-push", completed["prohibited_actions"])
            for key in (
                "turns_used",
                "tokens_used",
                "extensions_granted",
                "soft_turns",
                "hard_turns",
                "max_extensions",
            ):
                self.assertIn(key, completed["budget"])
            self.assertEqual(completed["budget"]["turns_used"], 3)
            self.assertEqual(completed["budget"]["tokens_used"], 175)
            self.assertGreaterEqual(completed["changed_path_count"], 1)

        asyncio.run(exercise())

    def test_token_capture(self) -> None:
        async def exercise() -> None:
            executor = UsageExecutor()
            manager = DevelopmentJobManager(
                self.settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertEqual(completed["budget"]["tokens_used"], 175)
            self.assertEqual(completed["budget"]["turns_used"], 3)
            self.assertTrue(completed["resume_available"])

        asyncio.run(exercise())

    def test_soft_budget_warning_flagged(self) -> None:
        async def exercise() -> None:
            # A single completed slice under the soft ceiling should not warn.
            settings = continuity_settings(self.base, CLAUDE_SOFT_TURNS="2")
            executor = UsageExecutor()  # num_turns=3 > soft_turns=2
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertTrue(completed["budget"]["soft_warned"])

        asyncio.run(exercise())


class CheckpointStoreTests(unittest.TestCase):
    """Direct tests of the append-only checkpoint ledger and compaction."""

    def _fresh_db(self) -> tuple[sqlite3.Connection, Path]:
        temporary = tempfile.TemporaryDirectory()
        self._tmp = temporary  # keep alive for the test
        db_path = Path(temporary.name) / "bridge.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_state_db(db_path)
        return connection, db_path

    def _row(self, job_id: str, kind: str, seq: int) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "project_id": "test-project",
            "kind": kind,
            "seq": seq,
            "recorded_at": "2026-08-07T00:00:00Z",
            "changed_paths_json": "[]",
            "partial_fingerprint": None,
            "tokens_json": None,
            "event_json": None,
            "milestone": None,
        }

    def test_compaction_keeps_milestone_and_boundary_checkpoints(self) -> None:
        connection, _ = self._fresh_db()
        try:
            job_id = "job-compact"
            # 60 sample rows (seq 0..59), 2 boundary rows, 1 milestone row.
            for seq in range(60):
                insert_checkpoint_row(connection, self._row(job_id, "sample", seq))
            insert_checkpoint_row(connection, self._row(job_id, "boundary", 60))
            insert_checkpoint_row(connection, self._row(job_id, "boundary", 61))
            insert_checkpoint_row(connection, self._row(job_id, "milestone", 62))
            self.assertEqual(checkpoint_count(connection, job_id), 63)

            deleted = __import__(
                "local_dev_mcp.store", fromlist=["compact_checkpoints"]
            ).compact_checkpoints(connection, job_id, keep_latest_n=10)
            self.assertEqual(deleted, 50)
            self.assertEqual(checkpoint_count(connection, job_id), 13)

            rows = load_checkpoint_rows(connection, job_id)
            kinds = [row["kind"] for row in rows]
            # All structural rows survive.
            self.assertEqual(kinds.count("boundary"), 2)
            self.assertEqual(kinds.count("milestone"), 1)
            # Only the 10 most-recent samples (seq 50..59) remain.
            sample_seqs = sorted(row["seq"] for row in rows if row["kind"] == "sample")
            self.assertEqual(sample_seqs, list(range(50, 60)))
        finally:
            connection.close()

    def test_additive_schema_upgrades_in_place(self) -> None:
        connection, db_path = self._fresh_db()
        try:
            connection.execute(
                "INSERT INTO jobs (job_id, project_id, effort, status, attempt, "
                "created_at, updated_at, cancel_requested) VALUES "
                "('legacyjob', 'test-project', 'high', 'completed', 1, 't', 't', 0)"
            )
            connection.commit()
            # Simulate a pre-continuity database: drop the continuity tables.
            connection.execute("DROP TABLE job_checkpoints")
            connection.execute("DROP TABLE job_events")
            connection.execute("DROP TABLE job_progress")
            connection.commit()
        finally:
            connection.close()

        # Re-opening with the continuity code recreates them additively, the
        # legacy job row survives, and the schema version is still 1.
        connection = open_state_db(db_path)
        try:
            for table in ("job_checkpoints", "job_events", "job_progress"):
                row = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                self.assertIsNotNone(row, f"{table} should be recreated on open")
            row = connection.execute(
                "SELECT status FROM jobs WHERE job_id='legacyjob'"
            ).fetchone()
            self.assertEqual(row["status"], "completed")
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
            self.assertEqual(int(version), 1)
        finally:
            connection.close()


class ContinuityToolSurfaceTests(unittest.TestCase):
    def test_continuity_adds_no_new_mcp_tools(self) -> None:
        import asyncio as _asyncio

        from local_dev_mcp.registry import ProjectRegistry
        from local_dev_mcp.server import create_server

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            initialize_git_repository(root)
            (root / "docs").mkdir()
            project = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test Project",
                    "description": "Continuity",
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
            )
            server = create_server(
                Settings.from_env(
                    {
                        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                        "JOB_WORKTREE_ROOT": str(base / "jobs"),
                    }
                ),
                ProjectRegistry({"test-project": project}),
            )
            tools = {tool.name for tool in _asyncio.run(server.list_tools())}

        # Continuity is internal to the job lifecycle: it must not add tools.
        self.assertEqual(len(tools), 23)
        for leaked in (
            "pause_development_job",
            "checkpoint_development_job",
            "extend_development_job",
            "compact_checkpoints",
            "list_checkpoints",
        ):
            self.assertNotIn(leaked, tools)
        self.assertIn("resume_development_job", tools)


if __name__ == "__main__":
    unittest.main()