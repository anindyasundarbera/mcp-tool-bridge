"""M-CC-11: sandboxed execution of repo-controlled verification scripts.

These tests verify the bubblewrap (bwrap) sandbox wrapper: a verification
command with ``sandbox: true`` is wrapped in a fixed bwrap argv (network none,
read-only root bind, frozen clock, die-with-parent), while an opted-out
command is unchanged. The real bwrap cannot create namespaces in this
environment (non-privileged user namespaces disabled), so the wrapper is
verified hermetically: the pure ``_sandbox_argv`` builder and a patched
``_run_exec`` that captures the argv actually passed to the subprocess.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy, VerificationCommand
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import VerificationCommitManager, _sandbox_argv

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


class SandboxArgvTests(unittest.TestCase):
    def test_sandbox_argv_wraps_command_with_fixed_bwrap_flags(self) -> None:
        worktree = type("W", (), {"path": Path("/tmp/worktree")})()
        argv = ["{python}", "-c", "print('hi')"]

        wrapped = _sandbox_argv(worktree, argv)  # type: ignore[arg-type]

        self.assertEqual(wrapped[0], "bwrap")
        self.assertIn("--unshare-net", wrapped)
        self.assertIn("--unshare-ipc", wrapped)
        self.assertIn("--unshare-pid", wrapped)
        self.assertIn("--die-with-parent", wrapped)
        # Read-only worktree bind; the whole host root is NOT bound.
        self.assertIn("--ro-bind", wrapped)
        self.assertIn("/tmp/worktree", wrapped)
        self.assertNotIn(("--ro-bind", "/", "/"), list(zip(wrapped, wrapped[1:])))
        # Only /dev/null is exposed as a device.
        self.assertIn("--dev-bind", wrapped)
        self.assertIn("/dev/null", wrapped)
        # Frozen clock.
        self.assertIn("--setenv", wrapped)
        self.assertIn("SOURCE_DATE_EPOCH", wrapped)
        self.assertIn("0", wrapped)
        # The original command is appended verbatim at the end.
        self.assertEqual(wrapped[-len(argv):], argv)


class SandboxedCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
        import subprocess

        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add source"], check=True
        )
        self.settings = settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _policy(self, sandbox: bool) -> ProjectPolicy:
        command = ["{python}", "-c", "import sys; sys.exit(0)"]
        return verification_project(
            self.root,
            command,
            verification_commands=[
                {
                    "id": "core",
                    "argv": command,
                    "python_path": ["src"],
                    "sandbox": sandbox,
                }
            ],
        )

    def test_sandboxed_command_wraps_argv_in_bwrap(self) -> None:
        async def exercise() -> None:
            policy = self._policy(sandbox=True)
            captured: dict[str, Any] = {}

            async def fake_run_exec(settings, worktree, **kwargs):
                captured["argv"] = kwargs["argv"]
                return {
                    "command_id": kwargs["command_id"],
                    "status": "passed",
                    "exit_code": 0,
                    "stdout": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                    "stderr": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                }

            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings,
                locks,
                EditingExecutor(lambda root: (root / "README.md").write_text("# C\n", encoding="utf-8")),  # type: ignore[arg-type]
                projects=(policy,),
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            completed = await wait_for_status(
                jobs, policy, str(started["job_id"]), {"completed"}
            )
            with patch(
                "local_dev_mcp.verification._run_exec", new=fake_run_exec
            ):
                verified = await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
                self.assertEqual(verified["status"], "passed", verified)
                await manager.commit(
                    policy,
                    str(completed["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    list(verified["paths"]),
                    "sandboxed commit",
                )

            self.assertEqual(captured["argv"][0], "bwrap")
            self.assertIn("--unshare-net", captured["argv"])
            self.assertIn("--die-with-parent", captured["argv"])

        asyncio.run(exercise())

    def test_opted_out_command_is_not_wrapped(self) -> None:
        async def exercise() -> None:
            policy = self._policy(sandbox=False)
            captured: dict[str, Any] = {}

            async def fake_run_exec(settings, worktree, **kwargs):
                captured["argv"] = kwargs["argv"]
                return {
                    "command_id": kwargs["command_id"],
                    "status": "passed",
                    "exit_code": 0,
                    "stdout": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                    "stderr": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                }

            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings,
                locks,
                EditingExecutor(lambda root: (root / "README.md").write_text("# C\n", encoding="utf-8")),  # type: ignore[arg-type]
                projects=(policy,),
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            completed = await wait_for_status(
                jobs, policy, str(started["job_id"]), {"completed"}
            )
            with patch(
                "local_dev_mcp.verification._run_exec", new=fake_run_exec
            ):
                verified = await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
                self.assertEqual(verified["status"], "passed", verified)
                await manager.commit(
                    policy,
                    str(completed["job_id"]),
                    str(verified["verification_id"]),
                    str(verified["base_head"]),
                    str(verified["diff_fingerprint"]),
                    list(verified["paths"]),
                    "unsandboxed commit",
                )

            self.assertNotEqual(captured["argv"][0], "bwrap")
            self.assertEqual(captured["argv"][0], "{python}")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
