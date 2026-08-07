import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import (
    DevelopmentJob,
    DevelopmentJobManager,
    JobStatus,
)
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager, ProjectRegistry, ProjectRegistryError
from local_dev_mcp.sync import SyncError, SyncManager

from tests.test_verification import EditingExecutor, wait_completed


def init_repo(root: Path) -> str:
    """Create a repo on ``main`` and return the initial HEAD (the stale base)."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Initial"], check=True)
    return rev(root, "HEAD")


def add_origin(root: Path, bare: Path) -> None:
    """Add a bare ``origin`` and point ``refs/remotes/origin/HEAD`` at main.

    The bridge's ``_remote_default_branch`` reads the ``origin/HEAD`` symref, so
    a plain ``git remote add`` is not enough; the symref must be set explicitly
    (clone normally sets it, ``remote add`` does not).
    """
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", str(bare)], check=True
    )
    subprocess.run(["git", "-C", str(root), "push", "-q", "origin", "main"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
        check=True,
    )


def advance_origin(bare: Path, base: Path, marker: str) -> str:
    """Simulate a merged PR: clone the bare remote, add a commit, push main.

    Returns the new remote tip. The canonical checkout is left untouched.
    """
    clone = base / f"clone-{marker}"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (clone / f"{marker}.md").write_text(f"# {marker}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-qm", f"Merge: {marker}"], check=True
    )
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)
    return rev(bare, "main")


def rev(root: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def branch(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def is_clean(root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == ""
    )


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Sync tests",
        "root": str(root),
        "capabilities": ["repository_read", "sync_canonical_checkout"],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


def jobs_policy(root: Path, **overrides: object) -> ProjectPolicy:
    """Policy that can also run dev jobs, for the job-interaction tests."""
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Sync tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "claude_edit",
            "verification_commit",
            "sync_canonical_checkout",
        ],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": ["{python}", "-c", "pass"]},
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


class _NoopExecutor(EditingExecutor):
    def __init__(self) -> None:
        super().__init__(edit=lambda root: None)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.base_head = init_repo(self.root)
        self.bare = self.base / "remote.git"
        add_origin(self.root, self.bare)
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _managers(
        self,
        policy_obj: ProjectPolicy,
        executor: Any | None = None,
    ) -> tuple[ProjectLockManager, DevelopmentJobManager, SyncManager]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            executor or _NoopExecutor(),
            projects=(policy_obj,),
        )
        sync = SyncManager(self.settings, locks, jobs, projects=(policy_obj,))
        return locks, jobs, sync

    # --- happy path -----------------------------------------------------

    def test_fast_forwards_canonical_checkout_to_verified_remote_tip(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-1")
            # Canonical checkout is still at the stale base.
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)
            self.assertEqual(branch(self.root), "main")

            result = await sync.sync(policy_obj, self.base_head, target)

            self.assertEqual(result["status"], "synchronized")
            self.assertFalse(result["idempotent"])
            self.assertEqual(result["from_head"], self.base_head)
            self.assertEqual(result["to_head"], target)
            self.assertEqual(result["branch"], "main")
            self.assertEqual(result["remote_url"], str(self.bare))
            self.assertEqual(result["warnings"], [])
            # The canonical checkout moved to the target; the remote is untouched.
            self.assertEqual(rev(self.root, "HEAD"), target)
            self.assertEqual(rev(self.root, "main"), target)
            self.assertEqual(rev(self.bare, "main"), target)
            self.assertEqual(branch(self.root), "main")
            self.assertTrue(is_clean(self.root))

        asyncio.run(exercise())

    def test_already_synchronized_is_idempotent_no_op(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            # Remote and canonical both still at base; target == current head.
            result = await sync.sync(policy_obj, self.base_head, self.base_head)
            self.assertEqual(result["status"], "already_synchronized")
            self.assertTrue(result["idempotent"])
            self.assertEqual(result["from_head"], self.base_head)
            self.assertEqual(result["to_head"], self.base_head)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)
            self.assertTrue(is_clean(self.root))

        asyncio.run(exercise())

    def test_sync_persists_audit_row_reloaded_by_new_manager(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            locks, jobs, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-2")
            await sync.sync(policy_obj, self.base_head, target)
            audit = sync.audit(policy_obj.id)
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["to_head"], target)
            self.assertFalse(audit[0]["idempotent"])

            # A fresh manager reading the same bridge.db re-hydrates the row.
            fresh = SyncManager(self.settings, locks, jobs, projects=(policy_obj,))
            self.assertEqual(len(fresh.audit(policy_obj.id)), 1)
            self.assertEqual(fresh.audit(policy_obj.id)[0]["to_head"], target)

        asyncio.run(exercise())

    # --- reject paths ---------------------------------------------------

    def test_rejects_dirty_canonical_checkout_without_discarding_changes(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-3")
            (self.root / "README.md").write_text("# dirty\n", encoding="utf-8")
            self.assertFalse(is_clean(self.root))

            with self.assertRaisesRegex(SyncError, "must be clean"):
                await sync.sync(policy_obj, self.base_head, target)

            # Nothing moved; the dirty change is preserved.
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)
            self.assertEqual((self.root / "README.md").read_text(), "# dirty\n")

        asyncio.run(exercise())

    def test_rejects_wrong_expected_current_head(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-4")
            wrong = "0" * 40
            with self.assertRaisesRegex(SyncError, "expected current head"):
                await sync.sync(policy_obj, wrong, target)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    def test_rejects_detached_head(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-5")
            subprocess.run(
                ["git", "-C", str(self.root), "checkout", "-q", "--detach", "main"],
                check=True,
            )
            self.assertEqual(branch(self.root), "")
            with self.assertRaisesRegex(SyncError, "detached HEAD"):
                await sync.sync(policy_obj, self.base_head, target)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    def test_rejects_non_default_branch(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-6")
            subprocess.run(
                ["git", "-C", str(self.root), "checkout", "-q", "-b", "feature"],
                check=True,
            )
            with self.assertRaisesRegex(SyncError, "default branch"):
                await sync.sync(policy_obj, self.base_head, target)
            self.assertEqual(branch(self.root), "feature")
            self.assertEqual(rev(self.root, "feature"), self.base_head)

        asyncio.run(exercise())

    def test_rejects_target_that_is_not_the_remote_tip(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            # Advance remote to one commit; craft a different real commit as target.
            _tip = advance_origin(self.bare, self.base, "merge-7a")
            other = advance_origin(self.bare, self.base, "merge-7b")
            # ``other`` is the real remote tip now; ``_tip`` is its parent and is
            # NOT the current tip. Use the parent as a real-but-wrong target.
            parent = rev(self.bare, "main^")
            self.assertNotEqual(parent, other)
            with self.assertRaisesRegex(SyncError, "not the current tip"):
                await sync.sync(policy_obj, self.base_head, parent)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    def test_rejects_divergent_local_history(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            target = advance_origin(self.bare, self.base, "merge-8")
            # Local diverges: a commit on canonical main that is not on origin.
            subprocess.run(
                ["git", "-C", str(self.root), "commit", "-q", "--allow-empty", "-m", "local"],
                check=True,
            )
            local_head = rev(self.root, "HEAD")
            self.assertNotEqual(local_head, self.base_head)
            with self.assertRaisesRegex(SyncError, "diverged"):
                await sync.sync(policy_obj, local_head, target)
            # The local commit is preserved.
            self.assertEqual(rev(self.root, "HEAD"), local_head)

        asyncio.run(exercise())

    def test_rejects_when_origin_is_not_configured(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            _, _, sync = self._managers(policy_obj)
            subprocess.run(
                ["git", "-C", str(self.root), "remote", "remove", "origin"], check=True
            )
            with self.assertRaisesRegex(SyncError, "origin"):
                await sync.sync(policy_obj, self.base_head, self.base_head)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    # --- job interaction ------------------------------------------------

    def test_blocks_while_a_non_terminal_job_exists(self) -> None:
        async def exercise() -> None:
            policy_obj = jobs_policy(self.root)
            _, jobs, sync = self._managers(policy_obj)
            # Inject a queued job directly: no task is scheduled, so the project
            # lock is free and sync acquires it, then refuses on the active job.
            queued = DevelopmentJob(
                job_id="injectedqueued000000000000aaaa",
                project_id=policy_obj.id,
                effort="high",
                status=JobStatus.QUEUED,
            )
            jobs._jobs[queued.job_id] = queued  # type: ignore[attr-defined]
            self.assertTrue(await jobs.has_active_jobs(policy_obj.id))

            target = advance_origin(self.bare, self.base, "merge-9")
            with self.assertRaisesRegex(SyncError, "non-terminal development job"):
                await sync.sync(policy_obj, self.base_head, target)
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    def test_warns_about_completed_uncommitted_job_that_loses_verify_path(self) -> None:
        async def exercise() -> None:
            policy_obj = jobs_policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, sync = self._managers(policy_obj, EditingExecutor(edit))
            started = await jobs.start(policy_obj, "make candidate", "high")
            completed = await wait_completed(jobs, policy_obj, str(started["job_id"]))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["base_head"], self.base_head)

            target = advance_origin(self.bare, self.base, "merge-10")
            result = await sync.sync(policy_obj, self.base_head, target)

            self.assertEqual(result["status"], "synchronized")
            self.assertEqual(result["to_head"], target)
            self.assertEqual(len(result["warnings"]), 1)
            warning = result["warnings"][0]
            self.assertEqual(warning["job_id"], started["job_id"])
            self.assertEqual(warning["base_head"], self.base_head)
            self.assertIn("verify/commit", warning["warning"])
            # The completed job's base no longer matches the canonical HEAD.
            self.assertEqual(rev(self.root, "HEAD"), target)
            self.assertNotEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())

    def test_no_warning_when_completed_job_base_matches_target(self) -> None:
        async def exercise() -> None:
            policy_obj = jobs_policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, sync = self._managers(policy_obj, EditingExecutor(edit))
            started = await jobs.start(policy_obj, "make candidate", "high")
            await wait_completed(jobs, policy_obj, str(started["job_id"]))

            # Idempotent sync to the current head (== the job's base): no warning.
            result = await sync.sync(policy_obj, self.base_head, self.base_head)
            self.assertEqual(result["status"], "already_synchronized")
            self.assertEqual(result["warnings"], [])

        asyncio.run(exercise())

    def test_has_active_jobs_and_stale_resumable_summaries_accessors(self) -> None:
        async def exercise() -> None:
            policy_obj = jobs_policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _sync = self._managers(policy_obj, EditingExecutor(edit))
            self.assertFalse(await jobs.has_active_jobs(policy_obj.id))
            self.assertEqual(await jobs.stale_resumable_job_summaries(policy_obj.id), [])

            started = await jobs.start(policy_obj, "make candidate", "high")
            await wait_completed(jobs, policy_obj, str(started["job_id"]))

            # Completed-uncommitted: not active, but stale-resumable.
            self.assertFalse(await jobs.has_active_jobs(policy_obj.id))
            summaries = await jobs.stale_resumable_job_summaries(policy_obj.id)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["base_head"], self.base_head)
            self.assertEqual(summaries[0]["job_id"], started["job_id"])

        asyncio.run(exercise())


class SyncCapabilityRegistryTests(unittest.TestCase):
    def test_sync_capability_requires_repository_read(self) -> None:
        with self.assertRaisesRegex(ValueError, "sync_canonical_checkout requires repository_read"):
            ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test Project",
                    "description": "x",
                    "root": "/tmp/does-not-matter",
                    "capabilities": ["sync_canonical_checkout"],
                }
            )

    def test_resolve_rejects_sync_for_project_without_capability(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            base = Path(temporary.name)
            root = base / "repo"
            init_repo(root)
            config = base / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "projects": [
                            {
                                "id": "test-project",
                                "display_name": "Test Project",
                                "description": "x",
                                "root": str(root),
                                "enabled": True,
                                "capabilities": ["repository_read"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = ProjectRegistry.load(config)
            with self.assertRaises(ProjectRegistryError):
                registry.resolve("test-project", ProjectCapability.SYNC_CANONICAL_CHECKOUT)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()