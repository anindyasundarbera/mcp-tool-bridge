"""M-CC-09: Verification-lab determinism — the hermetic verification lab.

Tests exercise the deterministic environment, flaky detection via
``deterministic_repetitions``, and baseline comparison against the last green
verification, with fake executors and real local commands (no network).
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import VerificationCommitManager

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, project, wait_completed


def hermetic_settings(base: Path, **env: str) -> Settings:
    payload = {
        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
        "JOB_WORKTREE_ROOT": str(base / "jobs"),
        "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
        "MAX_VERIFICATION_OUTPUT_BYTES": "1024",
    }
    payload.update(env)
    return Settings.from_env(payload)


class VerificationLabDeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "marker.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.root), "add", "src"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "Add source"], check=True
        )
        self.settings = hermetic_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def prepare(
        self,
        policy: ProjectPolicy,
        edit: Any,
    ) -> tuple[DevelopmentJobManager, VerificationCommitManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings, locks, EditingExecutor(edit), projects=(policy,)
        )
        manager = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_completed(jobs, policy, str(started["job_id"]))
        self.assertEqual(completed["status"], "completed")
        return jobs, manager, completed

    def test_deterministic_environment_variables_set(self) -> None:
        """The hermetic env is present: TZ=UTC, SOURCE_DATE_EPOCH=0, PYTHONHASHSEED=0."""
        async def exercise() -> None:
            command = [
                "{python}",
                "-c",
                "import os; print(os.environ.get('TZ')); "
                "print(os.environ.get('SOURCE_DATE_EPOCH')); "
                "print(os.environ.get('PYTHONHASHSEED'))",
            ]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed", verified)
            self.assertEqual(verified["commands"][0]["stdout"]["text"], "UTC\n0\n0\n")

        asyncio.run(exercise())

    def test_flaky_command_labeled_nondeterministic(self) -> None:
        """A command that flips its exit status across runs is labeled DEGRADED."""
        async def exercise() -> None:
            # Use the deterministic_repetitions>1 path with a flaky command that
            # exits 0 then 1 on alternate invocations (tracked via a marker file).
            self.settings = hermetic_settings(
                self.base,
                VERIFICATION_DETERMINISTIC_REPETITIONS="2",
            )
            command = [
                "{python}",
                "-c",
                (
                    "import os; f='flip.marker'; "
                    "n=int(open(f).read()) if os.path.exists(f) else 0; "
                    "open(f,'w').write(str(n+1)); "
                    "raise SystemExit(0 if n%2==0 else 1)"
                ),
            ]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["nondeterminism_labeled"], True)
            self.assertEqual(verified["infrastructure_status"], "DEGRADED")
            self.assertEqual(verified["deterministic_repetitions"], 2)

        asyncio.run(exercise())

    def test_deterministic_command_not_labeled(self) -> None:
        """A command that is stable across repetitions stays HEALTHY."""
        async def exercise() -> None:
            self.settings = hermetic_settings(
                self.base,
                VERIFICATION_DETERMINISTIC_REPETITIONS="3",
            )
            command = ["{python}", "-c", "print('stable')"]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["nondeterminism_labeled"], False)
            self.assertEqual(verified["infrastructure_status"], "HEALTHY")
            self.assertEqual(verified["deterministic_repetitions"], 3)

        asyncio.run(exercise())

    def test_baseline_comparison_reported(self) -> None:
        """A passing verification reports the baseline fingerprint + regression flag."""
        async def exercise() -> None:
            command = ["{python}", "-c", "print('ok')"]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            # First verification: no baseline yet.
            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["baseline_fingerprint"], None)
            self.assertEqual(verified["baseline_matches"], False)

            # A second, changed candidate: baseline now exists and diverges.
            def edit2(root: Path) -> None:
                (root / "README.md").write_text("# Changed candidate\n", encoding="utf-8")
                (root / "extra.md").write_text("extra\n", encoding="utf-8")

            jobs2, manager2, completed2 = await self.prepare(policy, edit2)
            # Reuse the same manager so the first record is already persisted.
            verified2 = await manager2.verify(
                policy, str(completed2["job_id"]), str(completed2["base_head"])
            )
            self.assertEqual(verified2["status"], "passed", verified2)
            self.assertIsNotNone(verified2["baseline_fingerprint"])
            self.assertEqual(verified2["baseline_matches"], False)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
