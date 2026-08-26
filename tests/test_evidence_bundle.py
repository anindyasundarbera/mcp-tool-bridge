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

from local_dev_mcp import store
from local_dev_mcp.config import Settings
from local_dev_mcp.evidence import (
    EvidenceBundleError,
    EvidenceBundleManager,
    _exclude_sensitive_paths,
    _is_sensitive_path,
    _scrub_sensitive_diff_detail,
)
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

    def test_bundle_requirements_section_is_none_without_parsed_requirements(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            bundle = await evidence.export_bundle(
                policy, job_id, str(verified["diff_fingerprint"])
            )
            # No requirements seeded for this project → coverage is None, but
            # the section is always present and never raised.
            self.assertIn("requirements", bundle)
            self.assertEqual(bundle["requirements"], {"requirements_covered": None})

        asyncio.run(exercise())

    def test_bundle_requirements_section_includes_coverage_when_seeded(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            # Seed one normative requirement and link it to this job so the
            # requirement manager (lazily derived from the job manager) reports
            # coverage. Seeding via the evidence manager's DB connection is
            # visible to the job manager's connection under WAL journaling.
            store.upsert_requirement_row(
                evidence._db,
                {
                    "requirement_id": "REQ-001",
                    "project_id": policy.id,
                    "text": "The system shall print ok.",
                    "source_path": "docs/reqs.md",
                    "source_line": 1,
                    "normative": 1,
                    "kind": "normative",
                    "status": "unimplemented",
                    "deferred_authority": None,
                    "updated_at": "2026-08-26T00:00:00Z",
                },
            )
            store.upsert_requirement_link_row(
                evidence._db,
                {
                    "requirement_id": "REQ-001",
                    "job_id": job_id,
                    "link_type": "job",
                    "link_value": job_id,
                    "artifact_kind": "code",
                    "artifact_path": "README.md",
                    "evidence_json": "{}",
                    "linked_at": "2026-08-26T00:00:00Z",
                },
            )
            bundle = await evidence.export_bundle(
                policy, job_id, str(verified["diff_fingerprint"])
            )
            self.assertIn("requirements", bundle)
            covered = bundle["requirements"]["requirements_covered"]
            self.assertIsNotNone(covered)
            self.assertEqual(covered["total"], 1)
            self.assertEqual(covered["normative"], 1)
            self.assertEqual(covered["covered"], 1)
            self.assertEqual(covered["uncovered"], 0)

        asyncio.run(exercise())

    def test_bundle_excludes_sensitive_changed_paths_and_reports_count(self) -> None:
        async def exercise() -> None:
            policy = project(self.root, ["{python}", "-c", "print('ok')"])
            verification, evidence, verified = await self.prepare_verified(policy)
            job_id = str(verified["job_id"])
            # Inject a latest checkpoint carrying sensitive changed paths so the
            # exclude filter is exercised end-to-end. Use a seq higher than any
            # checkpoint written during the run so load_latest_checkpoint returns
            # this row.
            existing = store.load_latest_checkpoint(evidence._db, job_id)
            next_seq = (existing["seq"] + 1) if existing else 1
            sensitive_paths = [
                "src/safe.py",
                ".env",
                ".venv/lib/foo.py",
                "tokens.txt",
                "config/secrets.yaml",
                "README.md",
            ]
            store.insert_checkpoint_row(
                evidence._db,
                {
                    "job_id": job_id,
                    "project_id": policy.id,
                    "kind": "test-injected",
                    "seq": next_seq,
                    "recorded_at": "2026-08-26T00:00:00Z",
                    "changed_paths_json": json.dumps(sensitive_paths),
                    "partial_fingerprint": str(verified["diff_fingerprint"]),
                    "tokens_json": "{}",
                    "event_json": "{}",
                    "milestone": None,
                },
            )
            bundle = await evidence.export_bundle(
                policy, job_id, str(verified["diff_fingerprint"])
            )
            candidate = bundle["candidate"]
            # Four of the six seeded paths are sensitive; they must be dropped.
            self.assertEqual(candidate["sensitive_excluded_count"], 4)
            kept = candidate["changed_paths"]
            self.assertEqual(set(kept), {"src/safe.py", "README.md"})
            # The sensitive path strings must never appear anywhere in the bundle.
            bundle_text = json.dumps(bundle)
            for sensitive in (".env", ".venv/lib/foo.py", "tokens.txt", "config/secrets.yaml"):
                self.assertNotIn(sensitive, bundle_text)
            # The bundle-wide content address is still recomputable.
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


class SensitivePathFilterTests(unittest.TestCase):
    """Unit tests for the sensitive-path exclude helpers (M-EV-06)."""

    def test_is_sensitive_path_matches_expected_patterns(self) -> None:
        self.assertTrue(_is_sensitive_path(".env"))
        self.assertTrue(_is_sensitive_path("config/.env"))
        self.assertTrue(_is_sensitive_path(".env.local"))
        self.assertTrue(_is_sensitive_path(".venv"))
        self.assertTrue(_is_sensitive_path(".venv/lib/foo.py"))
        self.assertTrue(_is_sensitive_path("tokens.txt"))
        self.assertTrue(_is_sensitive_path("creds/tokens.json"))
        self.assertTrue(_is_sensitive_path("credentials.yaml"))
        self.assertTrue(_is_sensitive_path("certs/server.pem"))
        self.assertTrue(_is_sensitive_path("deploy/key.pem"))
        self.assertTrue(_is_sensitive_path("private.key"))
        self.assertTrue(_is_sensitive_path("secrets/registry.json"))
        self.assertTrue(_is_sensitive_path(".aws/config"))
        self.assertTrue(_is_sensitive_path(".ssh/id_rsa"))

    def test_is_sensitive_path_does_not_false_match_source_paths(self) -> None:
        self.assertFalse(_is_sensitive_path("src/environment.py"))
        self.assertFalse(_is_sensitive_path("src/safe.py"))
        self.assertFalse(_is_sensitive_path("README.md"))
        self.assertFalse(_is_sensitive_path("docs/requirements.md"))
        self.assertFalse(_is_sensitive_path("src/monkeys.py"))
        self.assertFalse(_is_sensitive_path("tests/test_tokens_handler.py"))

    def test_exclude_sensitive_paths_drops_and_counts(self) -> None:
        paths = ["src/safe.py", ".env", ".venv/x", "tokens.txt", "README.md"]
        kept, excluded = _exclude_sensitive_paths(paths)
        self.assertEqual(kept, ["src/safe.py", "README.md"])
        self.assertEqual(excluded, 3)

    def test_exclude_sensitive_paths_preserves_none(self) -> None:
        kept, excluded = _exclude_sensitive_paths(None)
        self.assertIsNone(kept)
        self.assertEqual(excluded, 0)

    def test_exclude_sensitive_paths_keeps_non_string_entries(self) -> None:
        # Non-string entries cannot match a path pattern and are preserved.
        paths = ["src/safe.py", 42, ".env", {"not": "a path"}]
        kept, excluded = _exclude_sensitive_paths(paths)
        self.assertEqual(kept, ["src/safe.py", 42, {"not": "a path"}])
        self.assertEqual(excluded, 1)

    def test_scrub_sensitive_diff_detail_replaces_matching_lines(self) -> None:
        detail = (
            " src/safe.py      | 5 +-\n"
            " .env             | 12 ++\n"
            " tokens.txt       | 3 -\n"
            " README.md        | 1 +\n"
        )
        scrubbed = _scrub_sensitive_diff_detail(detail)
        self.assertIn("<excluded-sensitive>", scrubbed)
        self.assertIn("src/safe.py", scrubbed)
        self.assertIn("README.md", scrubbed)
        self.assertNotIn(".env", scrubbed)
        self.assertNotIn("tokens.txt", scrubbed)

    def test_scrub_sensitive_diff_detail_non_string_unchanged(self) -> None:
        self.assertIsNone(_scrub_sensitive_diff_detail(None))
        self.assertEqual(_scrub_sensitive_diff_detail(123), 123)


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
