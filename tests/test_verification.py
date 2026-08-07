import asyncio
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.git import GitCommandError
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import VerificationCommitManager, VerificationError
from local_dev_mcp import verification as verification_module
from tests.support import initialize_git_repository


def project(root: Path, command: list[str], **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Verification tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "claude_edit",
            "verification_commit",
        ],
        "commit_roots": ["."],
        "verification_commands": [
            {
                "id": "core",
                "argv": command,
                "python_path": ["src"],
            }
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


class EditingExecutor:
    def __init__(self, edit: Any) -> None:
        self.edit = edit

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, object]:
        assert root is not None
        self.edit(root)
        return {
            "status": "completed",
            "result": "candidate ready",
            "session_id": "verification-session",
        }


async def wait_completed(
    jobs: DevelopmentJobManager, project: ProjectPolicy, job_id: str
) -> dict[str, Any]:
    for _ in range(200):
        result = await jobs.get(project, job_id)
        if result["status"] in {"completed", "failed"}:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class VerificationCommitTests(unittest.TestCase):
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
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
                "MAX_VERIFICATION_OUTPUT_BYTES": "64",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
        completed = await wait_completed(jobs, policy, str(started["job_id"]))
        self.assertEqual(completed["status"], "completed")
        return jobs, manager, completed

    def test_verifies_and_commits_exact_state_without_moving_source_ref_or_hooks(self) -> None:
        async def exercise() -> None:
            command = [
                "{python}",
                "-c",
                (
                    "import os; from marker import VALUE; "
                    "print(os.getcwd()); print(VALUE); "
                    "print(','.join(sorted(os.environ)))"
                ),
            ]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")
                binary = root / "candidate.bin"
                binary.write_bytes(b"\x00candidate\xff")
                os.chmod(binary, 0o755)

            source_head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            hook_marker = self.base / "hook-ran"
            hook = self.root / ".git" / "hooks" / "post-commit"
            hook.write_text(f"#!/bin/sh\ntouch '{hook_marker}'\n", encoding="utf-8")
            hook.chmod(0o755)

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed", verified)
            self.assertEqual(verified["paths"], ["README.md", "candidate.bin"])
            self.assertRegex(str(verified["verification_id"]), r"^[0-9a-f]{32}$")
            self.assertRegex(str(verified["diff_fingerprint"]), r"^[0-9a-f]{64}$")
            self.assertNotIn(str(self.base), str(verified))
            self.assertEqual(verified["commands"][0]["status"], "passed")
            self.assertNotIn("HOME", verified["commands"][0]["stdout"]["text"])

            committed = await manager.commit(
                policy,
                str(completed["job_id"]),
                str(verified["verification_id"]),
                str(verified["base_head"]),
                str(verified["diff_fingerprint"]),
                list(verified["paths"]),
                "MCPB test candidate",
            )
            self.assertEqual(committed["status"], "committed")
            self.assertTrue(committed["detached"])
            self.assertFalse(committed["published"])
            self.assertEqual(committed["parent_head"], source_head)
            self.assertFalse(hook_marker.exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                source_head,
            )
            final_job = await jobs.get(policy, str(completed["job_id"]))
            self.assertFalse(final_job["resume_available"])
            self.assertEqual(final_job["commit_sha"], committed["commit_sha"])
            with self.assertRaisesRegex(VerificationError, "already been used"):
                await manager.commit(
                    policy,
                    str(completed["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    list(verified["paths"]),
                    "Replay",
                )

        asyncio.run(exercise())

    def test_failed_and_state_changing_commands_create_no_verification_authority(self) -> None:
        async def exercise(command: list[str]) -> dict[str, Any]:
            policy = project(self.root, command)
            _, manager, completed = await self.prepare(
                policy,
                lambda root: (root / "candidate.txt").write_text(
                    "candidate\n", encoding="utf-8"
                ),
            )
            return await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )

        failed = asyncio.run(
            exercise(["{python}", "-c", "print('x' * 1000); raise SystemExit(7)"])
        )
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("verification_id", failed)
        self.assertTrue(failed["commands"][0]["stdout"]["truncated"])
        self.assertEqual(failed["commands"][0]["exit_code"], 7)

        changed = asyncio.run(
            exercise(
                [
                    "{python}",
                    "-c",
                    "from pathlib import Path; Path('candidate.txt').write_text('changed')",
                ]
            )
        )
        self.assertFalse(changed["state_unchanged"])
        self.assertNotIn("verification_id", changed)

    def test_timeout_is_bounded_and_creates_no_verification_authority(self) -> None:
        async def exercise() -> dict[str, Any]:
            settings = Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                    "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "1",
                }
            )
            self.settings = settings
            policy = project(
                self.root,
                ["{python}", "-c", "import time; time.sleep(30)"],
            )
            _, manager, completed = await self.prepare(
                policy,
                lambda root: (root / "candidate.txt").write_text("x", encoding="utf-8"),
            )
            return await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )

        result = asyncio.run(exercise())
        self.assertEqual(result["commands"][0]["status"], "timed_out")
        self.assertNotIn("verification_id", result)

    def test_verification_rejects_staged_protected_outside_and_symlink_changes(self) -> None:
        async def rejection(
            policy: ProjectPolicy,
            edit: Any,
            expected: str,
            stage: bool = False,
        ) -> None:
            jobs, manager, completed = await self.prepare(policy, edit)
            if stage:
                worktree = await jobs.verification_worktree(
                    policy, str(completed["job_id"])
                )
                subprocess.run(
                    ["git", "-C", str(worktree.path), "add", "--", "candidate.txt"],
                    check=True,
                )
            with self.assertRaisesRegex(VerificationError, expected):
                await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )

        asyncio.run(
            rejection(
                project(self.root, ["{python}", "-c", "pass"]),
                lambda root: (root / "candidate.txt").write_text("x", encoding="utf-8"),
                "index must be empty",
                stage=True,
            )
        )
        asyncio.run(
            rejection(
                project(self.root, ["{python}", "-c", "pass"]),
                lambda root: (root / ".env").write_text("secret", encoding="utf-8"),
                "protected",
            )
        )
        asyncio.run(
            rejection(
                project(
                    self.root,
                    ["{python}", "-c", "pass"],
                    commit_roots=["src"],
                ),
                lambda root: (root / "candidate.txt").write_text("x", encoding="utf-8"),
                "outside approved",
            )
        )
        asyncio.run(
            rejection(
                project(self.root, ["{python}", "-c", "pass"]),
                lambda root: (root / ".hidden").write_text("x", encoding="utf-8"),
                "requires an explicit commit root",
            )
        )
        asyncio.run(
            rejection(
                project(self.root, ["{python}", "-c", "pass"]),
                lambda root: (root / "candidate-link").symlink_to("README.md"),
                "symbolic links",
            )
        )

    def test_commit_rejects_drift_and_non_exact_evidence_without_staging(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "pass"])
            jobs, manager, completed = await self.prepare(
                policy,
                lambda root: (root / "candidate.txt").write_text("one", encoding="utf-8"),
            )
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            worktree = await jobs.verification_worktree(
                policy, str(completed["job_id"])
            )
            before_head = completed["base_head"]
            with self.assertRaisesRegex(VerificationError, "paths must exactly"):
                await manager.commit(
                    policy,
                    str(completed["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    ["candidate.txt", "candidate.txt"],
                    "No",
                )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(worktree.path), "diff", "--cached", "--name-only"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            (worktree.path / "candidate.txt").write_text("two", encoding="utf-8")
            with self.assertRaisesRegex(VerificationError, "changed after verification"):
                await manager.commit(
                    policy,
                    str(completed["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    list(verified["paths"]),
                    "No",
                )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(worktree.path), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                before_head,
            )

        asyncio.run(exercise())

    def test_commit_failure_restores_empty_index_and_preserves_candidate(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "pass"])
            jobs, manager, completed = await self.prepare(
                policy,
                lambda root: (root / "candidate.txt").write_text(
                    "candidate", encoding="utf-8"
                ),
            )
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            worktree = await jobs.verification_worktree(
                policy, str(completed["job_id"])
            )
            original_run_git = verification_module.run_git

            async def fail_commit(root: Path, *arguments: str, **kwargs: Any) -> str:
                if "commit" in arguments:
                    raise GitCommandError("injected commit failure")
                return await original_run_git(root, *arguments, **kwargs)

            with patch.object(verification_module, "run_git", new=fail_commit):
                with self.assertRaisesRegex(VerificationError, "injected commit failure"):
                    await manager.commit(
                        policy,
                        str(completed["job_id"]),
                        str(verified["verification_id"]),
                        str(verified["base_head"]),
                        str(verified["diff_fingerprint"]),
                        list(verified["paths"]),
                        "Must fail",
                    )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(worktree.path), "diff", "--cached", "--name-only"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                "",
            )
            self.assertEqual((worktree.path / "candidate.txt").read_text(), "candidate")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(worktree.path), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                completed["base_head"],
            )

        asyncio.run(exercise())

    def test_fingerprint_covers_deletion_and_executable_mode(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "src" / "marker.py").unlink()
                readme = root / "README.md"
                readme.chmod(readme.stat().st_mode | stat.S_IXUSR)

            _, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed")
            self.assertEqual(verified["paths"], ["README.md", "src/marker.py"])

        asyncio.run(exercise())

    def test_candidate_state_closes_fd_when_ls_tree_fails(self) -> None:
        # Regression guard: the candidate file descriptor is opened before the
        # per-path ``ls-tree HEAD`` baseline call. If that call raises (timeout
        # or non-zero git), the already-open fd must be closed by the
        # ``try/finally`` around the open->``os.fdopen`` window — otherwise the
        # fd leaks on every such failure.
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "pass"])
            jobs, manager, completed = await self.prepare(
                policy,
                lambda root: (root / "candidate.txt").write_text(
                    "candidate", encoding="utf-8"
                ),
            )
            worktree = await jobs.verification_worktree(
                policy, str(completed["job_id"])
            )

            tracked_fd, tracked_path = tempfile.mkstemp(dir=self.base)
            os.write(tracked_fd, b"x")
            os.lseek(tracked_fd, 0, os.SEEK_SET)
            self.addCleanup(lambda: self._silently_close(tracked_fd))

            original_git_bytes = verification_module._git_bytes

            async def fail_ls_tree(root: Path, *arguments: str, **kwargs: Any) -> bytes:
                if "ls-tree" in arguments:
                    raise VerificationError("injected ls-tree failure")
                return await original_git_bytes(root, *arguments, **kwargs)

            with patch.object(verification_module, "_git_bytes", new=fail_ls_tree):
                with patch.object(
                    verification_module, "_open_candidate_read", return_value=tracked_fd
                ):
                    with self.assertRaisesRegex(
                        VerificationError, "injected ls-tree failure"
                    ):
                        await verification_module._candidate_state(
                            self.settings, policy, worktree
                        )

            # The finally must have closed the tracked fd: fstat now fails
            # with EBADF. If the leak returned, this fd would still be open.
            with self.assertRaises(OSError):
                os.fstat(tracked_fd)
            os.unlink(tracked_path)

        asyncio.run(exercise())

    @staticmethod
    def _silently_close(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

    def test_verification_waits_for_shared_project_lock(self) -> None:
        async def exercise() -> None:
            marker = self.base / "command-ran"
            policy = project(
                self.root,
                [
                    "{python}",
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ],
            )
            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings,
                locks,
                EditingExecutor(
                    lambda root: (root / "candidate.txt").write_text(
                        "candidate", encoding="utf-8"
                    )
                ),  # type: ignore[arg-type]
                projects=(policy,),
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "candidate", "high")
            completed = await wait_completed(jobs, policy, str(started["job_id"]))
            lock = await locks.for_project(policy.id)
            await lock.acquire()
            task = asyncio.create_task(
                manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(marker.exists())
            lock.release()
            result = await task
            self.assertEqual(result["status"], "passed")
            self.assertTrue(marker.exists())

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
