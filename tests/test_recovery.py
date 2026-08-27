"""M-CC-10: failure-recovery hardening.

Covers the four recovery capabilities with fake executors and real git
worktrees (no real Claude, no network), mirroring the patterns in
``tests/test_jobs.py``, ``tests/test_checkpoints.py`` and
``tests/test_verification.py``:

1. tool-failure retry within policy, then FAILED_INFRASTRUCTURE isolation;
2. candidate-corruption rollback to the last verified checkpoint;
3. lost-worktree reconstruction from the content-addressed checkpoint ledger;
4. compromised-provenance quarantine (terminal, non-resumable).
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.git import run_git
from local_dev_mcp.jobs import DevelopmentJobManager, JobStatus
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.store import (
    load_job_worktree_meta,
    load_latest_checkpoint,
)
from local_dev_mcp.verification import VerificationCommitManager, VerificationError

from tests.support import initialize_git_repository
from tests.test_jobs import (
    ImmediateExecutor,
    policy as job_policy,
    wait_for_status,
)


def recovery_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9101",
    }
    payload.update(env)
    return Settings.from_env(payload)


def verification_policy(
    root: Path, command: list[str], **overrides: object
) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Recovery tests",
        "root": str(root),
        "capabilities": ["repository_read", "claude_edit", "verification_commit"],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": command, "python_path": ["src"]}
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


class ToolFailureExecutor:
    """Fails with one tool failure for the first ``failures`` calls, then
    completes by writing ``candidate.txt`` (the retry-recovery model)."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
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
        n = len(self.calls) + 1
        self.calls.append(
            {
                "root": root,
                "text": specification,
                "resume_session_id": resume_session_id,
                "max_turns": max_turns,
            }
        )
        if n <= self.failures:
            return {
                "status": "failed",
                "tool_failures": 1,
                "num_turns": 1,
                "result": f"infra failure {n}",
            }
        (root / "candidate.txt").write_text(f"attempt {n}\n", encoding="utf-8")
        return {
            "status": "completed",
            "result": f"changed {root}",
            "session_id": "bridge-session-1",
            "repository_after": {"clean": False, "working_tree": "?? candidate.txt"},
        }


class AlwaysToolFailureExecutor:
    """Every call reports a single tool failure (the isolation model)."""

    def __init__(self) -> None:
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
        return {
            "status": "failed",
            "tool_failures": 1,
            "num_turns": 1,
            "result": f"infra failure {self.calls}",
        }


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        # A source directory the verification command's ``python_path`` resolves
        # in the job worktree (mirrors tests/test_verification.py).
        (self.root / "src").mkdir()
        (self.root / "src" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add source"], check=True
        )
        self.settings = recovery_settings(self.base)
        self.project = job_policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tool_failure_retry_recovers_within_policy(self) -> None:
        """A slice reporting tool failures is retried (fresh executor call with
        the same text) and a later successful attempt completes the job."""

        async def exercise() -> None:
            settings = recovery_settings(
                self.base,
                CLAUDE_TOOL_FAILURE_RETRIES="2",
                CLAUDE_MAX_TOOL_FAILURES="5",
            )
            executor = ToolFailureExecutor(failures=1)
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor, projects=(self.project,)
            )
            started = await manager.start(self.project, "work", "high")
            completed = await wait_for_status(
                manager, self.project, str(started["job_id"]), {"completed"}
            )
            self.assertEqual(len(executor.calls), 2)
            # Both attempts passed the same text (fresh invocation, no resume).
            self.assertEqual(executor.calls[0]["text"], "work")
            self.assertEqual(executor.calls[1]["text"], "work")
            self.assertIsNone(executor.calls[0]["resume_session_id"])
            self.assertIsNone(executor.calls[1]["resume_session_id"])
            # The failed attempt's tool failures were absorbed but did not
            # exceed the ceiling, so the job completed.
            self.assertEqual(completed["budget"]["tool_failures"], 1)
            self.assertEqual(completed["status"], "completed")

        asyncio.run(exercise())

    def test_tool_failure_retries_then_isolates_as_failed_infrastructure(self) -> None:
        """After the retry budget is exhausted the cumulative tool-failure
        ceiling isolates the job as FAILED_INFRASTRUCTURE (terminal,
        non-resumable)."""

        async def exercise() -> None:
            settings = recovery_settings(
                self.base,
                CLAUDE_TOOL_FAILURE_RETRIES="2",
                CLAUDE_MAX_TOOL_FAILURES="3",
            )
            executor = AlwaysToolFailureExecutor()
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor, projects=(self.project,)
            )
            started = await manager.start(self.project, "work", "high")
            isolated = await wait_for_status(
                manager,
                self.project,
                str(started["job_id"]),
                {"failed_infrastructure"},
            )
            # 1 initial call + 2 retries; the ceiling (3) stopped the retries.
            self.assertEqual(executor.calls, 3)
            self.assertEqual(isolated["budget"]["tool_failures"], 3)
            self.assertIn("tool failures", str(isolated["pause_reason"]))
            self.assertFalse(isolated["resume_available"])

        asyncio.run(exercise())

    def test_tool_failure_retries_disabled_isolates_immediately(self) -> None:
        """CLAUDE_TOOL_FAILURE_RETRIES=0 preserves the pre-M-CC-10 behavior: a
        single tool-failing slice is not retried and the job isolates."""

        async def exercise() -> None:
            settings = recovery_settings(
                self.base,
                CLAUDE_TOOL_FAILURE_RETRIES="0",
                CLAUDE_MAX_TOOL_FAILURES="5",
            )
            executor = AlwaysToolFailureExecutor()
            manager = DevelopmentJobManager(
                settings, ProjectLockManager(), executor, projects=(self.project,)
            )
            started = await manager.start(self.project, "work", "high")
            terminal = await wait_for_status(
                manager, self.project, str(started["job_id"]), {"failed"}
            )
            self.assertEqual(executor.calls, 1)
            self.assertEqual(terminal["budget"]["tool_failures"], 1)
            self.assertEqual(terminal["status"], "failed")

        asyncio.run(exercise())

    def test_candidate_corruption_rolls_back_to_last_verified_checkpoint(self) -> None:
        """A candidate that cannot be re-derived (staged index) is rolled back:
        the worktree returns to the clean base state the checkpoint ledger
        describes and the job pauses resumably instead of dangling."""

        async def exercise() -> None:
            policy = verification_policy(self.root, ["{python}", "-c", "pass"])
            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings, locks, ImmediateExecutor(), projects=(policy,)
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            completed = await wait_for_status(
                jobs, policy, str(started["job_id"]), {"completed"}
            )
            job_id = str(started["job_id"])
            checkpoint = load_latest_checkpoint(jobs._db, job_id)
            self.assertIsNotNone(checkpoint)
            # Corrupt the candidate: stage it so ``_candidate_state`` can no
            # longer re-derive the candidate (staged-index signature).
            worktree = await jobs.verification_worktree(policy, job_id)
            (worktree.path / "candidate.txt").write_text("one\n", encoding="utf-8")
            await run_git(worktree.path, "add", "candidate.txt")
            with self.assertRaisesRegex(
                VerificationError, "candidate index must be empty"
            ):
                await manager.verify(policy, job_id, str(completed["base_head"]))
            rolled = await jobs.get(policy, job_id)
            self.assertEqual(rolled["status"], "paused_interrupted")
            self.assertIn(
                "candidate corruption rolled back", str(rolled["pause_reason"])
            )
            self.assertTrue(rolled["resume_available"])
            # The worktree was reset to the clean base: nothing staged, no
            # tracked/untracked changes, HEAD back at base.
            status = await run_git(
                worktree.path, "status", "--porcelain=v1", "--untracked-files=all"
            )
            self.assertEqual(status, "")
            head = await run_git(worktree.path, "rev-parse", "HEAD")
            self.assertEqual(head.lower(), str(completed["base_head"]).lower())

        asyncio.run(exercise())

    def test_lost_worktree_is_reconstructed_from_checkpoint(self) -> None:
        """A job worktree whose directory vanished is rebuilt from the durable
        base commit + the last checkpoint's changed-set, and the job becomes
        resumable from the reconstructed (clean, detached) worktree."""

        async def exercise() -> None:
            settings = recovery_settings(
                self.base, CLAUDE_CHECKPOINT_INTERVAL_SECONDS="0.01"
            )
            executor = ToolFailureExecutor(failures=0)  # completes immediately
            jobs = DevelopmentJobManager(
                settings, ProjectLockManager(), executor, projects=(self.project,)
            )
            started = await jobs.start(self.project, "work", "high")
            completed = await wait_for_status(
                jobs, self.project, str(started["job_id"]), {"completed"}
            )
            job_id = str(started["job_id"])
            worktree = await jobs.verification_worktree(self.project, job_id)
            self.assertTrue(worktree.path.is_dir())
            # Lose the worktree: remove it (directory + git registration).
            await run_git(
                self.root,
                "worktree",
                "remove",
                "--force",
                "--",
                str(worktree.path),
            )
            self.assertFalse(worktree.path.exists())
            # The durable meta sidecar still records the base commit.
            meta = load_job_worktree_meta(jobs._db, job_id)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["base_head"].lower(), completed["base_head"].lower())

            summary = await jobs.reconstruct_lost_worktree(self.project, job_id)
            self.assertEqual(summary["status"], "reconstructed")
            self.assertEqual(
                summary["base_head"].lower(), completed["base_head"].lower()
            )
            rebuilt = await jobs.get(self.project, job_id)
            self.assertEqual(rebuilt["status"], "paused_interrupted")
            self.assertIn(
                "lost worktree reconstructed", str(rebuilt["pause_reason"])
            )
            self.assertTrue(rebuilt["resume_available"])
            # The recreated worktree exists, is detached at base, and is clean.
            new_meta = load_job_worktree_meta(jobs._db, job_id)
            new_path = Path(str(new_meta["worktree_path"]))
            self.assertTrue(new_path.is_dir())
            head = await run_git(new_path, "rev-parse", "HEAD")
            self.assertEqual(head.lower(), completed["base_head"].lower())
            status = await run_git(
                new_path, "status", "--porcelain=v1", "--untracked-files=all"
            )
            self.assertEqual(status, "")

        asyncio.run(exercise())

    def test_compromised_provenance_quarantines_job(self) -> None:
        """M-CT-05 real-path: a fingerprint mismatch at commit time transitions
        the job to QUARANTINED_PROVENANCE (terminal, non-resumable), not just
        raising."""

        async def exercise() -> None:
            policy = verification_policy(self.root, ["{python}", "-c", "pass"])
            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings, locks, ImmediateExecutor(), projects=(policy,)
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            completed = await wait_for_status(
                jobs, policy, str(started["job_id"]), {"completed"}
            )
            verified = await manager.verify(
                policy, str(started["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed")
            worktree = await jobs.verification_worktree(
                policy, str(started["job_id"])
            )
            # Corrupt the candidate after verification so the commit's
            # candidate-state re-derivation detects the fingerprint mismatch.
            (worktree.path / "candidate.txt").write_text(
                "corrupted", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                VerificationError, "changed after verification"
            ):
                await manager.commit(
                    policy,
                    str(started["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    list(verified["paths"]),
                    "Quarantine test",
                )
            quarantined = await jobs.get(policy, str(started["job_id"]))
            self.assertEqual(quarantined["status"], "quarantined_provenance")
            self.assertFalse(quarantined["resume_available"])

        asyncio.run(exercise())

    def test_reload_persists_worktree_base_before_nulling(self) -> None:
        """A terminal job whose worktree directory vanishes still has its base
        commit durably recorded in the sidecar after a fresh manager reload, so
        a later reconstruction is restart-safe."""

        async def exercise() -> None:
            jobs = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                ImmediateExecutor(),  # type: ignore[arg-type]
                projects=(self.project,),
            )
            started = await jobs.start(self.project, "work", "high")
            completed = await wait_for_status(
                jobs, self.project, str(started["job_id"]), {"completed"}
            )
            job_id = str(started["job_id"])
            worktree = await jobs.verification_worktree(self.project, job_id)
            await run_git(
                self.root,
                "worktree",
                "remove",
                "--force",
                "--",
                str(worktree.path),
            )
            # A fresh manager reloads the durable store: the worktree dir is
            # gone so the job is marked failed, but the meta sidecar survives.
            reloaded = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                ImmediateExecutor(),  # type: ignore[arg-type]
                projects=(self.project,),
            )
            reloaded_snapshot = await reloaded.get(self.project, job_id)
            self.assertEqual(reloaded_snapshot["status"], "failed")
            meta = load_job_worktree_meta(reloaded._db, job_id)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["base_head"].lower(), completed["base_head"].lower())
            # The job is still reconstructable from the durable ledger.
            summary = await reloaded.reconstruct_lost_worktree(self.project, job_id)
            self.assertEqual(summary["status"], "reconstructed")
            rebuilt = await reloaded.get(self.project, job_id)
            self.assertEqual(rebuilt["status"], "paused_interrupted")

        asyncio.run(exercise())
