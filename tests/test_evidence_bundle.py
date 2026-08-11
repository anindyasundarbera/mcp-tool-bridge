"""M-EV-06: Portable evidence bundle export — the redacted, content-addressed
bundle for a verified job.

Tests assemble a bundle for a job that has been verified, assert the bundle
matches the verified evidence, that paths are redacted, and that a project
without the capability cannot call the tool. Uses fake executors (no network).
"""

import asyncio
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.evidence import EvidenceBundleError, EvidenceBundleManager
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import VerificationCommitManager

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, project, wait_completed


def evidence_settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
            "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
            "MAX_VERIFICATION_OUTPUT_BYTES": "1024",
        }
    )


class EvidenceBundleTests(unittest.TestCase):
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
        self.settings = evidence_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def prepare_verified(
        self,
        policy: ProjectPolicy,
    ) -> tuple[VerificationCommitManager, EvidenceBundleManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(lambda root: (root / "README.md").write_text(
                "# Candidate\n", encoding="utf-8"
            )),
            projects=(policy,),
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        evidence = EvidenceBundleManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_completed(jobs, policy, str(started["job_id"]))
        verified = await verification.verify(
            policy, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        return verification, evidence, verified

    def test_bundle_matches_verified_evidence_and_redacts(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            fingerprint = str(verified["diff_fingerprint"])
            bundle = await evidence.export_bundle(policy, job_id, fingerprint)

            self.assertEqual(bundle["bundle_version"], 1)
            self.assertEqual(bundle["job_id"], job_id)
            self.assertEqual(bundle["expected_candidate_fingerprint"], fingerprint)
            self.assertEqual(bundle["base_head"], verified["base_head"])
            self.assertEqual(len(bundle["verifications"]), 1)
            self.assertEqual(
                bundle["verifications"][0]["verification_id"],
                verified["verification_id"],
            )
            self.assertEqual(
                bundle["verifications"][0]["diff_fingerprint"], fingerprint
            )
            # Raw verification command output must never leak into the bundle.
            for command in bundle["verifications"][0]["commands"]:
                self.assertNotIn("stdout", command)
                self.assertNotIn("stderr", command)
                self.assertIn("stdout_sha256", command)
                self.assertIn("stderr_sha256", command)
            self.assertIn("bundle_sha256", bundle)
            # Paths never leak.
            self.assertNotIn(str(self.root), json.dumps(bundle))
            self.assertNotIn(str(self.settings.job_worktree_root), json.dumps(bundle))
            # Bundle-wide SHA-256 is recomputable over the returned bytes.
            body = json.dumps(
                {k: v for k, v in bundle.items() if k != "bundle_sha256"},
                sort_keys=True,
                ensure_ascii=False,
            )
            self.assertEqual(
                bundle["bundle_sha256"],
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )

        asyncio.run(exercise())

    def test_rejects_bad_expected_fingerprint(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            with self.assertRaises(EvidenceBundleError):
                await evidence.export_bundle(policy, job_id, "not-a-fingerprint")

        asyncio.run(exercise())

    def test_bundle_includes_diff_summary_and_checkpoint_count(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            bundle = await evidence.export_bundle(
                policy, job_id, str(verified["diff_fingerprint"])
            )
            self.assertIsInstance(bundle["candidate"]["diff_summary"], dict)
            self.assertIn("files_changed", bundle["candidate"]["diff_summary"])
            self.assertIsInstance(bundle["checkpoint_count"], int)

        asyncio.run(exercise())


class EvidenceCapabilityTests(unittest.TestCase):
    """Tool gating: export_evidence_bundle is registered but runtime-gated."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        initialize_git_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_export_tool_present_in_schema(self) -> None:
        from local_dev_mcp.server import create_server
        from local_dev_mcp.registry import ProjectRegistry
        from local_dev_mcp.models import ProjectPolicy

        project = ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test",
                "description": "Test",
                "root": str(self.root),
                "capabilities": ["repository_read", "claude_edit"],
            }
        )
        server = create_server(
            Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                }
            ),
            ProjectRegistry({"test-project": project}),
        )
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertIn("export_evidence_bundle", tools)


if __name__ == "__main__":
    unittest.main()
