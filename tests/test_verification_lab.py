"""M-CC-09: Verification-lab determinism — the hermetic verification lab.

Tests exercise the deterministic environment, flaky detection via
``deterministic_repetitions``, and baseline comparison against the last green
verification, with fake executors and real local commands (no network).
"""

import asyncio
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

    def test_baseline_matches_on_identical_candidate_repeat(self) -> None:
        """M-CC-09: an identical candidate against the last green verification
        is flagged as a baseline match (no false-positive mismatch)."""
        async def exercise() -> None:
            command = ["{python}", "-c", "print('ok')"]
            policy = project(self.root, command)

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            first = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(first["status"], "passed", first)
            self.assertIsNone(first["baseline_fingerprint"])

            # An identical second candidate (same edit) is a deterministic
            # repeat: the baseline exists and matches the current fingerprint.
            jobs2, manager2, completed2 = await self.prepare(policy, edit)
            second = await manager2.verify(
                policy, str(completed2["job_id"]), str(completed2["base_head"])
            )
            self.assertEqual(second["status"], "passed", second)
            self.assertEqual(second["baseline_fingerprint"], first["diff_fingerprint"])
            self.assertTrue(second["baseline_matches"])

        asyncio.run(exercise())

    # -- M-CC-09 hermetic lab: pinned toolchain -----------------------------

    def test_pinned_toolchain_is_used(self) -> None:
        """M-CC-09: a pinned toolchain's interpreter runs the verification
        command instead of the bridge's interpreter."""
        async def exercise() -> None:
            pinned = self.base / "pinned-venv"
            (pinned / "bin").mkdir(parents=True)
            fake_python = pinned / "bin" / "python"
            fake_python.write_text(
                "#!/bin/sh\nexec {} -c \"print('PINNED-TOOLCHAIN')\"\n".format(
                    shlex.quote(sys.executable)
                ),
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            policy = project(
                self.root,
                ["{python}", "-c", "print('UNUSED')"],
                toolchain_pin=str(pinned),
            )

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed", verified)
            # The pinned interpreter printed the marker, not the declared
            # ``-c`` body, proving the pin replaced ``{python}``.
            self.assertEqual(
                verified["commands"][0]["stdout"]["text"], "PINNED-TOOLCHAIN\n"
            )
            # The toolchain-pin digest is a stable SHA-256 and never leaks the
            # pin path itself.
            self.assertRegex(str(verified["toolchain_pin_digest"]), r"^[0-9a-f]{64}$")
            self.assertNotIn(str(pinned), str(verified))

        asyncio.run(exercise())

    # -- M-CC-09 hermetic lab: network policy -------------------------------

    def test_network_policy_none_fails_closed_for_unsandboxed_command(self) -> None:
        """M-CC-09: with ``network_policy=none`` a command that cannot be
        given a no-network namespace (sandbox: false) fails closed — no
        verification authority is granted."""
        async def exercise() -> None:
            policy = project(
                self.root,
                ["{python}", "-c", "pass"],
                network_policy="none",
            )

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "failed")
            self.assertNotIn("verification_id", verified)
            self.assertEqual(verified["commands"][0]["status"], "failed")
            self.assertIn(
                "network_policy=none", str(verified["commands"][0]["error"])
            )
            self.assertEqual(verified["network_policy"], "none")

        asyncio.run(exercise())

    def test_network_policy_none_sandboxed_command_is_network_isolated(self) -> None:
        """M-CC-09: with ``network_policy=none`` a sandboxed command runs
        inside bwrap with ``--unshare-net`` (no network namespace)."""
        async def exercise() -> None:
            command = ["{python}", "-c", "pass"]
            policy = project(
                self.root,
                command,
                network_policy="none",
                verification_commands=[
                    {
                        "id": "core",
                        "argv": command,
                        "python_path": ["src"],
                        "sandbox": True,
                    }
                ],
            )
            captured: dict[str, Any] = {}

            async def fake_run_exec(settings: Settings, worktree: Any, **kwargs: Any) -> dict[str, Any]:
                captured["argv"] = kwargs["argv"]
                return {
                    "command_id": kwargs["command_id"],
                    "status": "passed",
                    "exit_code": 0,
                    "stdout": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                    "stderr": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                }

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            with patch("local_dev_mcp.verification._run_exec", new=fake_run_exec):
                verified = await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
            self.assertEqual(verified["status"], "passed", verified)
            self.assertEqual(captured["argv"][0], "bwrap")
            self.assertIn("--unshare-net", captured["argv"])

        asyncio.run(exercise())

    def test_network_policy_allowlist_pins_dns_to_allowlisted_hosts(self) -> None:
        """M-CC-09: with ``network_policy=allowlist`` a sandboxed command gets
        a generated hosts file that pins only the allowlisted hosts to loopback
        and an empty resolv.conf, so DNS for any other host fails closed."""
        async def exercise() -> None:
            command = ["{python}", "-c", "pass"]
            policy = project(
                self.root,
                command,
                network_policy="allowlist",
                network_allowlist=["cache.internal"],
                verification_commands=[
                    {
                        "id": "core",
                        "argv": command,
                        "python_path": ["src"],
                        "sandbox": True,
                    }
                ],
            )
            captured: dict[str, Any] = {}

            async def fake_run_exec(settings: Settings, worktree: Any, **kwargs: Any) -> dict[str, Any]:
                argv = kwargs["argv"]
                captured["argv"] = argv
                for index, token in enumerate(argv):
                    if token == "/etc/hosts":
                        hosts_path = argv[index - 1]
                        captured["hosts"] = Path(hosts_path).read_text()
                        break
                return {
                    "command_id": kwargs["command_id"],
                    "status": "passed",
                    "exit_code": 0,
                    "stdout": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                    "stderr": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                }

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            with patch("local_dev_mcp.verification._run_exec", new=fake_run_exec):
                verified = await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
            self.assertEqual(verified["status"], "passed", verified)
            self.assertEqual(captured["argv"][0], "bwrap")
            # Allowlist mode: the network namespace is SHARED (no --unshare-net)
            # so the loopback proxy is reachable, but DNS is pinned.
            self.assertNotIn("--unshare-net", captured["argv"])
            self.assertIn("/etc/hosts", captured["argv"])
            self.assertIn("/etc/resolv.conf", captured["argv"])
            self.assertIn("127.0.0.1 cache.internal", captured["hosts"])
            self.assertNotIn("example.com", captured["hosts"])

        asyncio.run(exercise())

    # -- M-CC-09 hermetic lab: read-only deps cache --------------------------

    def test_deps_cache_dir_exposed_read_only_for_sandboxed_command(self) -> None:
        """M-CC-09: a configured deps cache dir is exposed as DEPS_CACHE_DIR
        and bound read-only into the sandbox (read, never write)."""
        async def exercise() -> None:
            cache = self.base / "deps-cache"
            cache.mkdir()
            command = [
                "{python}",
                "-c",
                "import os; print(os.environ['DEPS_CACHE_DIR'])",
            ]
            policy = project(
                self.root,
                command,
                deps_cache_dir=str(cache),
                verification_commands=[
                    {
                        "id": "core",
                        "argv": command,
                        "python_path": ["src"],
                        "sandbox": True,
                    }
                ],
            )
            captured: dict[str, Any] = {}

            async def fake_run_exec(settings: Settings, worktree: Any, **kwargs: Any) -> dict[str, Any]:
                captured["argv"] = kwargs["argv"]
                captured["extra_env"] = kwargs.get("extra_env")
                return {
                    "command_id": kwargs["command_id"],
                    "status": "passed",
                    "exit_code": 0,
                    "stdout": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                    "stderr": {"text": "", "bytes": 0, "sha256": "", "truncated": False},
                }

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            with patch("local_dev_mcp.verification._run_exec", new=fake_run_exec):
                verified = await manager.verify(
                    policy, str(completed["job_id"]), str(completed["base_head"])
                )
            self.assertEqual(verified["status"], "passed", verified)
            self.assertEqual(captured["extra_env"]["DEPS_CACHE_DIR"], str(cache))
            # The cache is ro-bind mounted inside the sandbox (read-only).
            self.assertEqual(captured["argv"][0], "bwrap")
            self.assertIn("--ro-bind", captured["argv"])
            self.assertIn(str(cache), captured["argv"])

        asyncio.run(exercise())

    def test_deps_cache_dir_fails_closed_for_unsandboxed_command(self) -> None:
        """M-CC-09: a deps cache dir cannot guarantee read-only for a
        non-sandboxed command, so it fails closed."""
        async def exercise() -> None:
            cache = self.base / "deps-cache"
            cache.mkdir()
            policy = project(
                self.root,
                ["{python}", "-c", "pass"],
                deps_cache_dir=str(cache),
            )

            def edit(root: Path) -> None:
                (root / "README.md").write_text("# Candidate\n", encoding="utf-8")

            jobs, manager, completed = await self.prepare(policy, edit)
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "failed")
            self.assertNotIn("verification_id", verified)
            self.assertIn(
                "deps_cache_dir requires a sandboxed", str(verified["commands"][0]["error"])
            )

        asyncio.run(exercise())


class VerificationLabPolicyValidationTests(unittest.TestCase):
    """M-CC-09: the hermetic-lab policy is self-consistent — incoherent opt-ins
    fail closed at policy validation time."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _policy(self, **overrides: object) -> ProjectPolicy:
        return project(self.root, ["{python}", "-c", "pass"], **overrides)

    def test_allowlist_mode_requires_a_non_empty_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a non-empty network_allowlist"):
            self._policy(network_policy="allowlist")

    def test_none_mode_rejects_an_allowlist(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not configure a network_allowlist"):
            self._policy(network_policy="none", network_allowlist=["host.test"])

    def test_allowlist_without_network_policy_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires network_policy 'allowlist'"):
            self._policy(network_allowlist=["host.test"])

    def test_toolchain_pin_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            self._policy(toolchain_pin="relative/venv")

    def test_deps_cache_dir_must_be_absolute(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            self._policy(deps_cache_dir="relative/cache")


if __name__ == "__main__":
    unittest.main()
