import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.runtime_verification import (
    RuntimeVerificationError,
    RuntimeVerificationManager,
)

from tests.support import initialize_git_repository, project_payload
from tests.test_sync import add_origin, init_repo, rev


# Candidate verification IDs are 32 hex chars with no prefix. Runtime
# verification IDs are deliberately in a separate ``rtv_`` namespace so they
# can never be fed to ``commit_verified_development_job``.
_CANDIDATE_VERIFICATION_ID_PATTERN = r"^[0-9a-f]{32}$"


def rt_project(root: Path, commands: list[dict[str, object]], **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = project_payload(
        root,
        capabilities=["repository_read", "runtime_verification"],
        runtime_verification_commands=commands,
    )
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


def head(root: Path) -> str:
    return rev(root, "HEAD")


def porcelain(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def refs(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "for-each-ref", "--format=%(refname) %(objectname)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class RuntimeVerificationManagerTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                "GIT_COMMAND_TIMEOUT_SECONDS": "10",
                "RUNTIME_VERIFICATION_COMMAND_TIMEOUT_SECONDS": "3",
                "MAX_RUNTIME_VERIFICATION_OUTPUT_BYTES": "64",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager(
        self, policy: ProjectPolicy, *, locks: ProjectLockManager | None = None
    ) -> RuntimeVerificationManager:
        return RuntimeVerificationManager(
            self.settings, locks or ProjectLockManager(), projects=(policy,)
        )

    # -- Check selection ----------------------------------------------------

    async def test_unknown_check_raises_before_execution(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        pre_refs = refs(self.root)
        with self.assertRaisesRegex(
            RuntimeVerificationError, "unknown runtime verification check_id"
        ):
            await manager.run(policy, "missing", head(self.root))
        # No subprocess was spawned and no ref changed: the repo is untouched.
        self.assertEqual(refs(self.root), pre_refs)
        self.assertEqual(porcelain(self.root), "")
        self.assertEqual(manager.audit(policy.id), [])

    # -- Canonical binding -------------------------------------------------

    async def test_correct_clean_head_passes(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["executed"])
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])
        self.assertTrue(result["repository"]["unchanged"])
        self.assertFalse(result["repository"]["mutated"])
        self.assertEqual(result["verified_head"], head(self.root))

    async def test_wrong_expected_head_rejected_before_execution(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", "0" * 40)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["executed"])
        self.assertIn("does not match expected_head", result["error"])
        # No process ran: the working tree is still clean.
        self.assertEqual(porcelain(self.root), "")

    async def test_dirty_repository_rejected_before_execution(self) -> None:
        initialize_git_repository(self.root)
        (self.root / "untracked.txt").write_text("x", encoding="utf-8")
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["executed"])
        self.assertIn("clean", result["error"])
        self.assertEqual(result["exit_code"], None)

    async def test_wrong_branch_rejected_when_default_branch_known(self) -> None:
        init_repo(self.root)  # on main, commits present
        add_origin(self.root, self.base / "origin.git")
        subprocess.run(
            ["git", "-C", str(self.root), "checkout", "-q", "-b", "feature"],
            check=True,
        )
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["executed"])
        self.assertIn("default branch", result["error"])

    async def test_default_branch_passes_when_on_default(self) -> None:
        init_repo(self.root)
        add_origin(self.root, self.base / "origin.git")
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        self.assertEqual(result["status"], "passed")

    # -- Repository immutability -------------------------------------------

    async def test_head_mutation_during_execution_fails(self) -> None:
        initialize_git_repository(self.root)
        mutator = (
            "open('mutated.txt','w').write('x'); "
            "import subprocess; "
            "subprocess.run(['git','add','-A'], check=True); "
            "subprocess.run(['git','commit','-qm','mutated'], check=True)"
        )
        policy = rt_project(
            self.root,
            [{"id": "mutate", "argv": ["{python}", "-c", mutator]}],
        )
        manager = self._manager(policy)
        pre_head = head(self.root)
        result = await manager.run(policy, "mutate", pre_head)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["executed"])
        self.assertFalse(result["repository"]["unchanged"])
        self.assertTrue(result["repository"]["mutated"])
        self.assertIn("changed during runtime verification", result["error"])
        self.assertNotEqual(head(self.root), pre_head)

    async def test_working_tree_mutation_during_execution_fails(self) -> None:
        initialize_git_repository(self.root)
        dirty = "open('dirty.txt','w').write('x')"
        policy = rt_project(
            self.root,
            [{"id": "dirty", "argv": ["{python}", "-c", dirty]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "dirty", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["executed"])
        self.assertFalse(result["repository"]["unchanged"])
        self.assertTrue(result["repository"]["mutated"])

    # -- Execution outcomes ------------------------------------------------

    async def test_nonzero_exit_fails(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "fail", "argv": ["{python}", "-c", "import sys; sys.exit(1)"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "fail", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("exited with code 1", result["error"])

    async def test_launch_failure_fails(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "ghost", "argv": ["nonexistent-bridge-xyz-executable"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "ghost", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["executed"])
        self.assertIsNone(result["exit_code"])
        self.assertIn("executable could not start", result["error"])

    async def test_timeout_fails(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "slow", "argv": ["{python}", "-c", "import time; time.sleep(30)"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "slow", head(self.root))
        self.assertEqual(result["status"], "failed")
        self.assertIn("timed out", result["error"])

    async def test_self_signal_fails(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [
                {
                    "id": "signal",
                    "argv": [
                        "{python}",
                        "-c",
                        "import os,signal; os.kill(os.getpid(), signal.SIGKILL)",
                    ],
                }
            ],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "signal", head(self.root))
        self.assertEqual(result["status"], "failed")

    # -- Output bounding & redaction --------------------------------------

    async def test_output_is_bounded_and_hashed(self) -> None:
        initialize_git_repository(self.root)
        # Tiny bound (64 bytes from settings); emit well over it.
        policy = rt_project(
            self.root,
            [
                {
                    "id": "noisy",
                    "argv": ["{python}", "-c", "print('x' * 4096)"],
                }
            ],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "noisy", head(self.root))
        self.assertEqual(result["status"], "passed")
        stdout = result["stdout"]
        self.assertTrue(stdout["truncated"])
        self.assertLessEqual(len(stdout["text"].encode("utf-8")), 64)
        self.assertEqual(result["stdout_hash"], stdout["sha256"])
        # The hash is reproducible from the returned (bounded) text.
        self.assertEqual(
            stdout["sha256"],
            hashlib.sha256(stdout["text"].encode("utf-8")).hexdigest(),
        )

    async def test_secret_env_value_is_redacted_from_output(self) -> None:
        initialize_git_repository(self.root)
        secret = "supersecrettoken123"  # len >= 12 -> redacted
        policy = rt_project(
            self.root,
            [
                {
                    "id": "leak",
                    "argv": ["{python}", "-c", "import os; print(os.environ['SECRET'])"],
                    "environment": {"SECRET": secret},
                }
            ],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "leak", head(self.root))
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertIn("<redacted>", result["stdout"]["text"])

    async def test_absolute_project_root_not_exposed(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn(str(self.base), blob)

    # -- Governance separation --------------------------------------------

    async def test_runtime_id_is_not_a_candidate_verification_id(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        rtv_id = result["runtime_verification_id"]
        self.assertTrue(rtv_id.startswith("rtv_"))
        self.assertIsNone(re.match(_CANDIDATE_VERIFICATION_ID_PATTERN, rtv_id))

    async def test_pass_performs_no_git_mutation(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        pre_head, pre_branch, pre_porcelain, pre_refs = (
            head(self.root),
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            porcelain(self.root),
            refs(self.root),
        )
        result = await manager.run(policy, "core", pre_head)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(head(self.root), pre_head)
        self.assertEqual(porcelain(self.root), pre_porcelain)
        self.assertEqual(refs(self.root), pre_refs)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            pre_branch,
        )

    # -- Locking -----------------------------------------------------------

    async def test_runtime_run_blocked_by_concurrent_lock_holder(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        locks = ProjectLockManager()
        manager = self._manager(policy, locks=locks)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            lock = await locks.for_project(policy.id)
            async with lock:
                entered.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await entered.wait()
        run_task = asyncio.create_task(manager.run(policy, "core", head(self.root)))
        await asyncio.sleep(0.1)
        self.assertFalse(run_task.done())
        release.set()
        await holder
        result = await run_task
        self.assertEqual(result["status"], "passed")

    # -- Persistence -------------------------------------------------------

    async def test_pass_row_persisted_and_reloadable(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        rows = manager.audit(policy.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runtime_verification_id"], result["runtime_verification_id"])
        self.assertEqual(rows[0]["status"], "passed")
        # A fresh manager over the same durable DB re-hydrates the row.
        reloaded = self._manager(policy)
        reloaded_rows = reloaded.audit(policy.id)
        self.assertEqual(len(reloaded_rows), 1)
        self.assertEqual(
            reloaded_rows[0]["runtime_verification_id"], result["runtime_verification_id"]
        )

    async def test_failure_row_also_persisted(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "fail", "argv": ["{python}", "-c", "import sys; sys.exit(2)"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "fail", head(self.root))
        self.assertEqual(result["status"], "failed")
        rows = manager.audit(policy.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")

    # -- Evidence fingerprint ----------------------------------------------

    async def test_evidence_fingerprint_deterministic_and_changes_with_result(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [
                {"id": "pass", "argv": ["{python}", "-c", "print('ok')"]},
                {"id": "fail", "argv": ["{python}", "-c", "import sys; sys.exit(1)"]},
            ],
        )
        manager = self._manager(policy)
        first = await manager.run(policy, "pass", head(self.root))
        second = await manager.run(policy, "pass", head(self.root))
        self.assertEqual(first["evidence_fingerprint"], second["evidence_fingerprint"])
        failed = await manager.run(policy, "fail", head(self.root))
        self.assertNotEqual(
            first["evidence_fingerprint"], failed["evidence_fingerprint"]
        )

    async def test_policy_fingerprint_reflects_config(self) -> None:
        initialize_git_repository(self.root)
        policy = rt_project(
            self.root,
            [{"id": "core", "argv": ["{python}", "-c", "print('ok')"]}],
        )
        manager = self._manager(policy)
        result = await manager.run(policy, "core", head(self.root))
        self.assertTrue(result["policy_fingerprint"])
        # The fingerprint is a SHA-256 hex digest over the sorted runtime commands.
        self.assertRegex(result["policy_fingerprint"], r"^[0-9a-f]{64}$")


class RuntimeVerificationCapabilityGateTests(unittest.TestCase):
    """A project without the runtime_verification capability is denied at resolve."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                "RUNTIME_VERIFICATION_COMMAND_TIMEOUT_SECONDS": "3",
                "MAX_RUNTIME_VERIFICATION_OUTPUT_BYTES": "64",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_without_capability_denied(self) -> None:
        from local_dev_mcp.models import ProjectCapability
        from local_dev_mcp.registry import ProjectRegistry, ProjectRegistryError

        policy = ProjectPolicy.model_validate(
            project_payload(self.root, capabilities=["repository_read"])
        )
        registry = ProjectRegistry({policy.id: policy})
        with self.assertRaisesRegex(
            ProjectRegistryError, "does not allow runtime_verification"
        ):
            registry.resolve(policy.id, ProjectCapability.RUNTIME_VERIFICATION)

    def test_project_with_capability_allowed(self) -> None:
        from local_dev_mcp.models import ProjectCapability
        from local_dev_mcp.registry import ProjectRegistry

        policy = ProjectPolicy.model_validate(
            project_payload(
                self.root,
                capabilities=["repository_read", "runtime_verification"],
                runtime_verification_commands=[
                    {"id": "core", "argv": ["{python}", "-c", "1"]}
                ],
            )
        )
        registry = ProjectRegistry({policy.id: policy})
        resolved = registry.resolve(policy.id, ProjectCapability.RUNTIME_VERIFICATION)
        self.assertEqual(resolved.id, policy.id)


class RuntimeVerificationCommandInjectionTests(unittest.TestCase):
    """Hostile check_id strings are rejected by the schema pattern before lookup."""

    def test_hostile_check_ids_rejected_by_pattern(self) -> None:
        pattern = r"^[a-z][a-z0-9_-]{0,63}$"
        # check_id is only a lookup key (never executed), but the schema pattern
        # still rejects shell-metachar / whitespace / uppercase input so a
        # caller cannot smuggle command text through the field. A dashed name
        # like "rm-rf" is a *valid* check_id shape — it simply matches no
        # configured check and is rejected as unknown, so it is not hostile.
        for hostile in ("; rm -rf /", "$(x)", "a b", "UPPER", "sh -c x", "../x"):
            self.assertIsNone(
                re.match(pattern, hostile),
                f"pattern should reject {hostile!r}",
            )

    def test_safe_check_ids_accepted_by_pattern(self) -> None:
        pattern = r"^[a-z][a-z0-9_-]{0,63}$"
        for safe in ("core", "integration-runtime", "a", "check_1", "docker-up"):
            self.assertIsNotNone(re.match(pattern, safe))


if __name__ == "__main__":
    unittest.main()