import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.worktrees import GitWorktreeManager, WorktreeError
from tests.support import initialize_git_repository
from tests.test_sync import add_origin, advance_origin, init_repo, rev


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
        }
    )


def policy(root: Path) -> ProjectPolicy:
    return ProjectPolicy.model_validate(
        {
            "id": "test-project",
            "display_name": "Test Project",
            "description": "Worktree tests",
            "root": str(root),
            "capabilities": ["repository_read", "claude_edit"],
        }
    )


class GitWorktreeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = settings(self.base)
        self.manager = GitWorktreeManager(self.settings)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_clean_detached_worktree_at_exact_head(self) -> None:
        base_head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        worktree = asyncio.run(self.manager.create(self.project, "a" * 32))

        self.assertEqual(worktree.base_head, base_head)
        self.assertEqual(
            worktree.path,
            (self.settings.job_worktree_root / "test-project" / ("a" * 32)).resolve(),
        )
        head = subprocess.run(
            ["git", "-C", str(worktree.path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(worktree.path), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(head, base_head)
        self.assertEqual(branch, "")
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "",
        )

    def test_dirty_staged_and_untracked_sources_fail_without_worktree(self) -> None:
        attempts = ("dirty", "staged", "untracked")
        for index, state in enumerate(attempts):
            with self.subTest(state=state):
                if state == "dirty":
                    (self.root / "README.md").write_text("changed\n", encoding="utf-8")
                else:
                    target = self.root / f"{state}.txt"
                    target.write_text(state, encoding="utf-8")
                    if state == "staged":
                        subprocess.run(
                            ["git", "-C", str(self.root), "add", target.name], check=True
                        )

                job_id = f"{index + 1:032x}"
                with self.assertRaisesRegex(WorktreeError, "must be clean"):
                    asyncio.run(self.manager.create(self.project, job_id))
                self.assertFalse(
                    (self.settings.job_worktree_root / "test-project" / job_id).exists()
                )

                subprocess.run(
                    ["git", "-C", str(self.root), "reset", "--hard", "-q", "HEAD"],
                    check=True,
                )
                for child in self.root.iterdir():
                    if child.name not in {".git", "README.md"}:
                        child.unlink()

    def test_rejects_symlinked_worktree_root_and_collision(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        self.settings.job_worktree_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(WorktreeError, "symbolic"):
            asyncio.run(self.manager.create(self.project, "b" * 32))
        self.assertEqual(list(outside.iterdir()), [])

        self.settings.job_worktree_root.unlink()
        collision = self.settings.job_worktree_root / "test-project" / ("c" * 32)
        collision.mkdir(parents=True)
        self.settings.job_worktree_root.chmod(0o700)
        collision.parent.chmod(0o700)
        with self.assertRaisesRegex(WorktreeError, "already exists"):
            asyncio.run(self.manager.create(self.project, "c" * 32))
        self.assertEqual(list(collision.iterdir()), [])

    def test_rejects_worktree_root_inside_project_before_creating_it(self) -> None:
        inside = self.root / "private-jobs"
        constrained = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(inside),
            }
        )
        with self.assertRaisesRegex(WorktreeError, "outside"):
            asyncio.run(
                GitWorktreeManager(constrained).create(self.project, "e" * 32)
            )
        self.assertFalse(inside.exists())

        other = self.base / "other-repo"
        initialize_git_repository(other)
        cross_project = other / "private-jobs"
        cross_settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(cross_project),
            }
        )
        with self.assertRaisesRegex(WorktreeError, "every configured project"):
            asyncio.run(
                GitWorktreeManager(cross_settings, (other,)).create(
                    self.project, "f" * 32
                )
            )
        self.assertFalse(cross_project.exists())

    def test_resume_rejects_dirty_or_moved_source_and_moved_worktree_head(self) -> None:
        worktree = asyncio.run(self.manager.create(self.project, "d" * 32))

        (self.root / "README.md").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(WorktreeError, "must be clean"):
            asyncio.run(self.manager.validate_resume(self.project, worktree))
        subprocess.run(
            ["git", "-C", str(self.root), "reset", "--hard", "-q", "HEAD"], check=True
        )

        (self.root / "next.txt").write_text("next\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "next.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Move source"], check=True
        )
        with self.assertRaisesRegex(WorktreeError, "HEAD moved"):
            asyncio.run(self.manager.validate_resume(self.project, worktree))

        subprocess.run(
            ["git", "-C", str(self.root), "reset", "--hard", "-q", worktree.base_head],
            check=True,
        )
        (worktree.path / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree.path), "add", "candidate.txt"], check=True
        )
        subprocess.run(
            ["git", "-C", str(worktree.path), "commit", "-qm", "Move worktree"],
            check=True,
        )
        with self.assertRaisesRegex(WorktreeError, "worktree HEAD moved"):
            asyncio.run(self.manager.validate_resume(self.project, worktree))


class StaleBaseGuardTests(unittest.TestCase):
    """The pre-work staleness guard in ``worktrees.create``."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.base_head = init_repo(self.root)
        self.bare = self.base / "remote.git"
        add_origin(self.root, self.bare)
        self.settings = settings(self.base)
        self.manager = GitWorktreeManager(self.settings)
        self.project = policy(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_refuses_stale_base_with_actionable_message(self) -> None:
        # A PR merged on the remote; the canonical checkout is left stale. The
        # guard reads local remote-tracking refs (no fetch), so simulate the
        # orchestrator's prepare_canonical_for_work having fetched first.
        advance_origin(self.bare, self.base, "merged-1")
        subprocess.run(["git", "-C", str(self.root), "fetch", "-q", "origin"], check=True)
        self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        with self.assertRaisesRegex(WorktreeError, "prepare_canonical_for_work"):
            asyncio.run(self.manager.create(self.project, "a" * 32))

        # No worktree was created; the canonical checkout is untouched.
        self.assertEqual(rev(self.root, "HEAD"), self.base_head)
        self.assertFalse(
            (self.settings.job_worktree_root / "test-project" / ("a" * 32)).exists()
        )

    def test_create_does_not_fetch_on_hot_path(self) -> None:
        # A merged PR exists on the remote, but the local tracking ref has NOT
        # been refreshed (no fetch). The non-fetching guard must no-op rather
        # than block on the network, so worktree creation proceeds at the stale
        # base — the orchestrator is expected to have called
        # prepare_canonical_for_work first to arm the guard.
        advance_origin(self.bare, self.base, "merged-no-fetch")
        self.assertEqual(rev(self.root, "HEAD"), self.base_head)
        self.assertEqual(
            rev(self.root, "refs/remotes/origin/main"), self.base_head
        )

        worktree = asyncio.run(self.manager.create(self.project, "e" * 32))
        self.assertEqual(worktree.base_head, self.base_head)

    def test_create_succeeds_after_sync_clears_staleness(self) -> None:
        advance_origin(self.bare, self.base, "merged-2")
        # Simulate the orchestrator pulling via ff-only: fetch, then move local
        # main to the remote tip so the base is no longer stale.
        subprocess.run(["git", "-C", str(self.root), "fetch", "-q", "origin"], check=True)
        tip = rev(self.bare, "main")
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", "-q", tip],
            check=True,
        )
        worktree = asyncio.run(self.manager.create(self.project, "b" * 32))
        self.assertEqual(worktree.base_head, tip)

    def test_create_skips_guard_when_offline(self) -> None:
        # No origin remote: the guard is skipped and a worktree is created at
        # the local HEAD (existing contract preserved).
        solo = self.base / "solo"
        init_repo(solo)
        solo_project = policy(solo)
        worktree = asyncio.run(self.manager.create(solo_project, "c" * 32))
        self.assertEqual(worktree.base_head, rev(solo, "HEAD"))

    def test_create_skips_guard_for_pinned_requested_base(self) -> None:
        # Stale canonical checkout, but the caller pins requested_base to the
        # fetched remote tip: the guard is skipped and the pinned base is used.
        tip = advance_origin(self.bare, self.base, "merged-3")
        self.assertEqual(rev(self.root, "HEAD"), self.base_head)
        worktree = asyncio.run(
            self.manager.create(self.project, "d" * 32, requested_base=tip)
        )
        self.assertEqual(worktree.base_head, tip.lower())


if __name__ == "__main__":
    unittest.main()
