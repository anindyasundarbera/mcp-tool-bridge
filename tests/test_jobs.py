import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp import store
from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobError, DevelopmentJobManager
from local_dev_mcp.models import Effort, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from tests.support import initialize_git_repository


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "CANDIDATE_MCP_PORT": "9101",
        }
    )


def settings_with(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "CANDIDATE_MCP_PORT": "9101",
    }
    payload.update(env)
    return Settings.from_env(payload)


def policy(root: Path, project_id: str = "test-project", **overrides: object) -> ProjectPolicy:
    data: dict[str, object] = {
        "id": project_id,
        "display_name": "Test Project",
        "description": "Job tests",
        "root": str(root),
        "capabilities": ["repository_read", "claude_edit"],
    }
    data.update(overrides)
    return ProjectPolicy.model_validate(data)


async def wait_for_status(
    manager: DevelopmentJobManager,
    project: ProjectPolicy,
    job_id: str,
    expected: set[str],
) -> dict[str, Any]:
    for _ in range(200):
        snapshot = await manager.get(project, job_id)
        if str(snapshot["status"]) in expected:
            return snapshot
        await asyncio.sleep(0.01)
    raise AssertionError(f"job did not reach {expected}")


class ImmediateExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "root": root,
                "text": specification,
                "effort": effort,
                "resume_session_id": resume_session_id,
                "max_turns": max_turns,
            }
        )
        (root / "candidate.txt").write_text(
            f"attempt {len(self.calls)}\n", encoding="utf-8"
        )
        return {
            "status": "completed",
            "project_id": project.id,
            "result": f"changed {root}",
            "session_id": "bridge-session-1",
            "repository_after": {"clean": False, "working_tree": "?? candidate.txt"},
        }


class BlockingExecutor(ImmediateExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

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
        self.calls.append({"root": root, "resume_session_id": resume_session_id})
        (root / "partial.txt").write_text("partial\n", encoding="utf-8")
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class FakeCandidateChecker:
    def __init__(self, status: str = "healthy") -> None:
        self.status = status
        self.roots: list[Path] = []

    async def check(self, root: Path) -> dict[str, Any]:
        self.roots.append(root)
        result: dict[str, Any] = {
            "status": self.status,
            "port": 9101,
            "tools": ["send_to_local_dev", "get_development_job"],
        }
        if self.status != "healthy":
            result["error"] = f"failed under {root}"
        return result


class ParallelExecutor:
    def __init__(self) -> None:
        self.projects: set[str] = set()
        self.both_entered = asyncio.Event()
        self.release = asyncio.Event()

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
        self.projects.add(project.id)
        if len(self.projects) == 2:
            self.both_entered.set()
        await self.release.wait()
        return {"status": "completed", "result": "done"}


class DevelopmentJobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = settings(self.base)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_start_is_async_and_edits_only_the_retained_worktree(self) -> None:
        async def exercise() -> None:
            executor = BlockingExecutor()
            manager = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                executor,  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "approved work", "high")
            self.assertEqual(started["status"], "queued")
            self.assertRegex(str(started["job_id"]), r"^[0-9a-f]{32}$")

            await asyncio.wait_for(executor.entered.wait(), timeout=2)
            running = await manager.get(self.project, str(started["job_id"]))
            self.assertEqual(running["status"], "running")
            worktree_root = executor.calls[0]["root"]
            self.assertIsInstance(worktree_root, Path)
            self.assertNotEqual(worktree_root, self.root)
            self.assertFalse((self.root / "partial.txt").exists())

            cancelled = await manager.cancel(self.project, str(started["job_id"]))
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(executor.cancelled.is_set())
            self.assertTrue((worktree_root / "partial.txt").exists())  # type: ignore[operator]
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.root), "status", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )

        asyncio.run(exercise())

    def test_completion_redacts_paths_and_session_and_resume_reuses_session(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                executor,  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertTrue(completed["resume_available"])
            self.assertNotIn("session_id", str(completed))
            self.assertNotIn(str(self.root), str(completed))
            self.assertNotIn(str(self.settings.job_worktree_root), str(completed))
            self.assertEqual(executor.calls[0]["resume_session_id"], None)
            self.assertFalse((self.root / "candidate.txt").exists())

            resumed = await manager.resume(
                self.project, job_id, "continue", "ultracode"
            )
            self.assertEqual(resumed["status"], "queued")
            self.assertEqual(resumed["attempt"], 2)
            completed_again = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertTrue(completed_again["resume_available"])
            self.assertEqual(executor.calls[1]["resume_session_id"], "bridge-session-1")
            self.assertEqual(executor.calls[0]["root"], executor.calls[1]["root"])

        asyncio.run(exercise())

    def test_resume_and_lookup_fail_closed(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                executor,  # type: ignore[arg-type]
            )
            started = await manager.start(self.project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(manager, self.project, job_id, {"completed"})

            other_root = self.base / "other"
            initialize_git_repository(other_root)
            other = policy(other_root, "other-project")
            with self.assertRaisesRegex(DevelopmentJobError, "unknown job_id"):
                await manager.get(other, job_id)

            subprocess.run(
                ["git", "-C", str(self.root), "commit", "--allow-empty", "-qm", "Move"],
                check=True,
            )
            await manager.resume(self.project, job_id, "continue", "high")
            failed = await wait_for_status(manager, self.project, job_id, {"failed"})
            self.assertIn("HEAD moved", str(failed["error"]))
            self.assertEqual(len(executor.calls), 1)

        asyncio.run(exercise())

    def test_dirty_baseline_fails_before_executor_and_shared_lock_queues(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            locks = ProjectLockManager()
            manager = DevelopmentJobManager(
                self.settings,
                locks,
                executor,  # type: ignore[arg-type]
            )
            project_lock = await locks.for_project(self.project.id)
            await project_lock.acquire()
            started = await manager.start(self.project, "queued", "high")
            await asyncio.sleep(0.05)
            self.assertEqual(len(executor.calls), 0)
            self.assertEqual(
                (await manager.get(self.project, str(started["job_id"])))["status"],
                "queued",
            )
            project_lock.release()
            await wait_for_status(
                manager, self.project, str(started["job_id"]), {"completed"}
            )

            (self.root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            dirty = await manager.start(self.project, "must fail", "high")
            failed = await wait_for_status(
                manager, self.project, str(dirty["job_id"]), {"failed"}
            )
            self.assertIn("must be clean", str(failed["error"]))
            self.assertEqual(len(executor.calls), 1)

        asyncio.run(exercise())

    def test_self_managed_job_requires_candidate_health(self) -> None:
        async def exercise(status: str) -> dict[str, Any]:
            executor = ImmediateExecutor()
            checker = FakeCandidateChecker(status)
            manager = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                executor,  # type: ignore[arg-type]
                candidate_checker=checker,  # type: ignore[arg-type]
            )
            self_managed = policy(self.root, self_managed=True)
            started = await manager.start(self_managed, "self update", "high")
            result = await wait_for_status(
                manager,
                self_managed,
                str(started["job_id"]),
                {"completed", "failed"},
            )
            self.assertEqual(len(checker.roots), 1)
            self.assertNotEqual(checker.roots[0], self.root)
            return result

        healthy = asyncio.run(exercise("healthy"))
        self.assertEqual(healthy["status"], "completed")
        self.assertEqual(healthy["candidate"]["status"], "healthy")

        unhealthy = asyncio.run(exercise("unhealthy"))
        self.assertEqual(unhealthy["status"], "failed")
        self.assertEqual(unhealthy["candidate"]["status"], "unhealthy")
        self.assertNotIn(str(self.settings.job_worktree_root), str(unhealthy))

    def test_independent_projects_can_run_concurrently(self) -> None:
        async def exercise() -> None:
            other_root = self.base / "other"
            initialize_git_repository(other_root)
            other = policy(other_root, "other-project")
            executor = ParallelExecutor()
            manager = DevelopmentJobManager(
                self.settings,
                ProjectLockManager(),
                executor,  # type: ignore[arg-type]
                projects=(self.project, other),
            )
            first = await manager.start(self.project, "first", "high")
            second = await manager.start(other, "second", "high")
            await asyncio.wait_for(executor.both_entered.wait(), timeout=2)
            self.assertEqual(executor.projects, {"test-project", "other-project"})
            executor.release.set()
            await wait_for_status(
                manager, self.project, str(first["job_id"]), {"completed"}
            )
            await wait_for_status(
                manager, other, str(second["job_id"]), {"completed"}
            )

        asyncio.run(exercise())


class TrackedEditExecutor:
    """Edits a tracked file so ``git diff --numstat HEAD`` reports the change.

    Unlike ``ImmediateExecutor`` (which creates an untracked ``candidate.txt``),
    this executor appends a line to the already-tracked ``README.md`` so the
    diff summary surfaces a real tracked change.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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
        self.calls.append({"root": root})
        original = (root / "README.md").read_text(encoding="utf-8")
        (root / "README.md").write_text(
            original + "candidate line\n", encoding="utf-8"
        )
        return {
            "status": "completed",
            "project_id": project.id,
            "result": "done",
            "session_id": "bridge-session-1",
        }


class DiffSummaryAndValidationEvidenceTests(unittest.TestCase):
    """M-CT-06: ``diff_summary`` and ``validation_evidence`` in the terminal report."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = settings(self.base)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(
        self, executor: Any
    ) -> DevelopmentJobManager:
        return DevelopmentJobManager(
            self.settings,
            ProjectLockManager(),
            executor,  # type: ignore[arg-type]
            projects=(self.project,),
        )

    def _verification_row(
        self, job_id: str, base_head: str, *, status: str = "passed"
    ) -> dict[str, Any]:
        return {
            "verification_id": "testverification0000000000000000001",
            "project_id": self.project.id,
            "job_id": job_id,
            "base_head": base_head,
            "diff_fingerprint": "a" * 64,
            "paths_json": json.dumps(["README.md"]),
            "verified_at": "2026-01-01T00:00:00Z",
            "used": 0,
            "commands_json": json.dumps(
                [{"command_id": "test", "status": status, "exit_code": 0 if status == "passed" else 1}]
            ),
        }

    def _review_row(self, job_id: str, base_head: str) -> dict[str, Any]:
        return {
            "review_id": "testreview000000000000000000000000001",
            "project_id": self.project.id,
            "job_id": job_id,
            "base_head": base_head,
            "diff_fingerprint": "a" * 64,
            "paths_json": json.dumps(["README.md"]),
            "reviewed_at": "2026-01-01T00:00:00Z",
            "reviewer_id": "human-reviewer",
            "verdict_json": json.dumps({"recommendation": "approve", "summary": "good"}),
            "used": 0,
        }

    def test_diff_summary_matches_known_tracked_change(self) -> None:
        async def exercise() -> None:
            executor = TrackedEditExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            self.assertIn("diff_summary", completed)
            self.assertEqual(completed["diff_summary"]["files_changed"], 1)
            self.assertEqual(completed["diff_summary"]["insertions"], 1)
            self.assertEqual(completed["diff_summary"]["deletions"], 0)
            # The absolute worktree path must never appear in the snapshot.
            self.assertNotIn(str(self.settings.job_worktree_root), str(completed))

        asyncio.run(exercise())

    def test_validation_evidence_present_when_verification_exists(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            store.upsert_record_row(
                manager._db,
                self._verification_row(job_id, completed["base_head"]),
            )
            snapshot = await manager.get(self.project, job_id)
            self.assertIn("validation_evidence", snapshot)
            self.assertIn("verifications", snapshot["validation_evidence"])
            verifs = snapshot["validation_evidence"]["verifications"]
            self.assertEqual(len(verifs), 1)
            self.assertEqual(
                verifs[0]["verification_id"],
                "testverification0000000000000000001",
            )
            self.assertEqual(verifs[0]["status"], "passed")
            self.assertFalse(verifs[0]["used"])

        asyncio.run(exercise())

    def test_validation_evidence_includes_review_verdict(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            store.upsert_review_row(
                manager._db,
                self._review_row(job_id, completed["base_head"]),
            )
            snapshot = await manager.get(self.project, job_id)
            self.assertIn("validation_evidence", snapshot)
            self.assertIn("reviews", snapshot["validation_evidence"])
            reviews = snapshot["validation_evidence"]["reviews"]
            self.assertEqual(len(reviews), 1)
            self.assertEqual(
                reviews[0]["review_id"],
                "testreview000000000000000000000000001",
            )
            self.assertEqual(reviews[0]["verdict_summary"]["recommendation"], "approve")
            self.assertFalse(reviews[0]["used"])

        asyncio.run(exercise())

    def test_verified_job_returns_both_diff_summary_and_validation_evidence(self) -> None:
        async def exercise() -> None:
            executor = TrackedEditExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            base_head = completed["base_head"]
            store.upsert_record_row(manager._db, self._verification_row(job_id, base_head))
            store.upsert_review_row(manager._db, self._review_row(job_id, base_head))
            snapshot = await manager.get(self.project, job_id)
            # Both enrichment fields are present on a verified job.
            self.assertIn("diff_summary", snapshot)
            self.assertIn("validation_evidence", snapshot)
            # diff_summary reflects the tracked change.
            self.assertEqual(snapshot["diff_summary"]["files_changed"], 1)
            self.assertEqual(snapshot["diff_summary"]["insertions"], 1)
            # validation_evidence carries both verifications and reviews.
            self.assertIn("verifications", snapshot["validation_evidence"])
            self.assertIn("reviews", snapshot["validation_evidence"])
            self.assertEqual(
                snapshot["validation_evidence"]["verifications"][0]["status"], "passed"
            )
            self.assertEqual(
                snapshot["validation_evidence"]["reviews"][0]["verdict_summary"]["recommendation"],
                "approve",
            )

        asyncio.run(exercise())

    def test_no_verification_row_omits_validation_evidence(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            # A completed job with no verification/review row must NOT emit
            # validation_evidence (no empty placeholder that reads as "verified").
            self.assertNotIn("validation_evidence", completed)
            # diff_summary is still present (the worktree exists).
            self.assertIn("diff_summary", completed)

        asyncio.run(exercise())

    def test_failed_verification_status_is_reported(self) -> None:
        async def exercise() -> None:
            executor = ImmediateExecutor()
            manager = self._manager(executor)
            started = await manager.start(self.project, "approved work", "high")
            job_id = str(started["job_id"])
            completed = await wait_for_status(
                manager, self.project, job_id, {"completed"}
            )
            store.upsert_record_row(
                manager._db,
                self._verification_row(
                    job_id, completed["base_head"], status="failed"
                ),
            )
            snapshot = await manager.get(self.project, job_id)
            self.assertIn("validation_evidence", snapshot)
            self.assertEqual(
                snapshot["validation_evidence"]["verifications"][0]["status"], "failed"
            )

        asyncio.run(exercise())


class EffortResolutionTests(unittest.TestCase):
    """Omitting ``effort`` resolves via project default -> CLAUDE_DEFAULT_EFFORT."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(self, settings_obj: Settings) -> tuple[DevelopmentJobManager, "ImmediateExecutor"]:
        executor = ImmediateExecutor()
        manager = DevelopmentJobManager(
            settings_obj,
            ProjectLockManager(),
            executor,  # type: ignore[arg-type]
        )
        return manager, executor

    def test_project_default_effort_used_when_call_omits(self) -> None:
        async def exercise() -> None:
            project = policy(self.root, default_effort="ultracode", allow_ultracode=True)
            manager, executor = self._manager(settings(self.base))
            started = await manager.start(project, "work", None)
            await wait_for_status(
                manager, project, str(started["job_id"]), {"completed"}
            )
            self.assertEqual(executor.calls[0]["effort"], "ultracode")

        asyncio.run(exercise())

    def test_global_default_used_when_project_omits(self) -> None:
        async def exercise() -> None:
            # default_effort omitted -> None -> inherits CLAUDE_DEFAULT_EFFORT.
            project = policy(self.root, allow_ultracode=True)
            manager, executor = self._manager(
                settings_with(self.base, CLAUDE_DEFAULT_EFFORT="ultracode")
            )
            started = await manager.start(project, "work", None)
            await wait_for_status(
                manager, project, str(started["job_id"]), {"completed"}
            )
            self.assertEqual(executor.calls[0]["effort"], "ultracode")

        asyncio.run(exercise())

    def test_no_overrides_resolve_to_high(self) -> None:
        async def exercise() -> None:
            project = policy(self.root)  # default_effort None, allow_ultracode True
            manager, executor = self._manager(settings(self.base))
            started = await manager.start(project, "work", None)
            await wait_for_status(
                manager, project, str(started["job_id"]), {"completed"}
            )
            self.assertEqual(executor.calls[0]["effort"], "high")

        asyncio.run(exercise())

    def test_explicit_call_effort_wins_over_project_default(self) -> None:
        async def exercise() -> None:
            project = policy(self.root, default_effort="ultracode", allow_ultracode=True)
            manager, executor = self._manager(settings(self.base))
            started = await manager.start(project, "work", "high")
            await wait_for_status(
                manager, project, str(started["job_id"]), {"completed"}
            )
            self.assertEqual(executor.calls[0]["effort"], "high")

        asyncio.run(exercise())

    def test_global_ultracode_fails_closed_for_disallowing_project(self) -> None:
        async def exercise() -> None:
            project = policy(self.root, allow_ultracode=False)
            manager, _ = self._manager(
                settings_with(self.base, CLAUDE_DEFAULT_EFFORT="ultracode")
            )
            with self.assertRaisesRegex(ValueError, "does not allow ultracode"):
                await manager.start(project, "work", None)

        asyncio.run(exercise())

    def test_resume_resolves_none_effort(self) -> None:
        async def exercise() -> None:
            project = policy(self.root, default_effort="ultracode", allow_ultracode=True)
            manager, executor = self._manager(settings(self.base))
            started = await manager.start(project, "first", "high")
            job_id = str(started["job_id"])
            await wait_for_status(manager, project, job_id, {"completed"})
            # Resume a completed job (fresh attempt, retained worktree) omitting
            # effort; the project default (ultracode) should be applied.
            await manager.resume(project, job_id, "continue", None)
            await wait_for_status(manager, project, job_id, {"completed"})
            self.assertEqual(executor.calls[1]["effort"], "ultracode")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
