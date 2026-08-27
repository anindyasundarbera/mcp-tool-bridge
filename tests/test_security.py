"""M-CC-11: security hardening (tail) — opt-in, tested, no new tools.

The milestone ships four per-project hardening mechanisms, all opt-in and
internal:

- ``filesystem_policy`` (read/write allowlists) enforced in the reader and
  writer surfaces;
- ``credential_policy`` (short-lived, least-privilege credential projection
  into worktree subprocess environments only);
- ``signed_manifests`` (deterministic HMAC-SHA256 evidence-bundle signing with
  a project key, stored alongside the bundle);
- ``sensitivity_policy`` (classify artifact sensitivity before export and
  redact/exclude with a count).

The supply-chain gate itself is covered in ``test_supply_chain.py``; here we
cover its M-CC-11 integration (credential projection into the scanner reaches
the real commit gate) plus the three new items above. Uses fake executors (no
network).
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from local_dev_mcp import security, store
from local_dev_mcp.config import Settings
from local_dev_mcp.evidence import (
    EvidenceBundleError,
    EvidenceBundleManager,
    _exclude_sensitive_paths,
    _scrub_sensitive_diff_detail,
)
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import CredentialPolicy, FileSystemPolicy, ProjectPolicy
from local_dev_mcp.reader import RepositoryReadError, RepositoryReader
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.security import (
    filesystem_read_allowed,
    filesystem_write_allowed,
    projected_credential_env,
    project_sensitivity_prefixes,
    sign_evidence_bundle,
    verify_evidence_signature,
)
from local_dev_mcp.verification import VerificationCommitManager, VerificationError
from local_dev_mcp.writer import RepositoryWriteError, RepositoryWriter

from tests.support import initialize_git_repository
from tests.test_evidence_bundle import evidence_settings
from tests.test_jobs import wait_for_status
from tests.test_verification import EditingExecutor
from tests.test_writer import policy as writer_policy


def _repo(base: Path) -> Path:
    root = base / "repo"
    initialize_git_repository(root)
    (root / "src").mkdir()
    (root / "src" / "marker.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "src"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "Add source"], check=True
    )
    return root


def _verify_capabilities() -> list[str]:
    return ["repository_read", "claude_edit", "verification_commit"]


def _export_capabilities() -> list[str]:
    return [
        "repository_read",
        "claude_edit",
        "verification_commit",
        "export_evidence_bundle",
    ]


def _verification_command() -> list[dict[str, object]]:
    return [{"id": "core", "argv": ["{python}", "-c", "print('ok')"], "python_path": ["src"]}]


class SecurityPolicyValidationTests(unittest.TestCase):
    """The new per-project policies are opt-in and validated."""

    def _base(self, root: Path, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "test-project",
            "display_name": "Test Project",
            "description": "Security tests",
            "root": str(root),
            "capabilities": ["repository_read"],
        }
        payload.update(overrides)
        return payload

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hardening_is_opt_in_by_default(self) -> None:
        policy = ProjectPolicy.model_validate(self._base(self.base))
        self.assertIsNone(policy.filesystem_policy)
        self.assertIsNone(policy.credential_policy)
        self.assertIsNone(policy.sensitivity_policy)
        self.assertFalse(policy.signed_manifests)

    def test_filesystem_policy_accepts_allowlists_and_rejects_bad_shapes(self) -> None:
        policy = ProjectPolicy.model_validate(
            self._base(
                self.base,
                filesystem_policy={
                    "read_allow": ["docs", "src/**"],
                    "write_allow": ["docs/"],
                },
            )
        )
        self.assertEqual(policy.filesystem_policy.read_allow, ("docs", "src/**"))
        self.assertEqual(policy.filesystem_policy.write_allow, ("docs",))
        # A trailing ``**`` is honored but ``a/**/b`` is not.
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(
                    self.base,
                    filesystem_policy={"read_allow": ["docs/**/b"]},
                )
            )
        # An empty allowlist is meaningless.
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(self.base, filesystem_policy={})
            )

    def test_credential_policy_accepts_names_and_rejects_bad_names(self) -> None:
        policy = ProjectPolicy.model_validate(
            self._base(
                self.base,
                credential_policy={"allow": ["GH_TOKEN", "GITHUB_TOKEN"]},
            )
        )
        self.assertEqual(policy.credential_policy.allow, ("GH_TOKEN", "GITHUB_TOKEN"))
        # Duplicate names are rejected.
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(
                    self.base,
                    credential_policy={"allow": ["GH_TOKEN", "GH_TOKEN"]},
                )
            )
        # Deterministic env names must never be projected (M-CC-09).
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(self.base, credential_policy={"allow": ["TZ"]})
            )
        # Interpreter/loader rebind names are forbidden.
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(self.base, credential_policy={"allow": ["PYTHONPATH"]})
            )

    def test_sensitivity_policy_validated(self) -> None:
        policy = ProjectPolicy.model_validate(
            self._base(
                self.base,
                capabilities=_export_capabilities(),
                commit_roots=["."],
                verification_commands=_verification_command(),
                sensitivity_policy={"sensitive_paths": ["config/secrets/**"]},
            )
        )
        self.assertEqual(
            policy.sensitivity_policy.sensitive_paths, ("config/secrets/**",)
        )
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(
                    self.base,
                    capabilities=_export_capabilities(),
                    commit_roots=["."],
                    verification_commands=_verification_command(),
                    sensitivity_policy={"sensitive_paths": []},
                )
            )

    def test_signed_manifests_requires_export_evidence_bundle(self) -> None:
        with self.assertRaises(Exception):
            ProjectPolicy.model_validate(
                self._base(self.base, signed_manifests=True)
            )
        # With the export capability it validates.
        ProjectPolicy.model_validate(
            self._base(
                self.base,
                signed_manifests=True,
                capabilities=_export_capabilities(),
                commit_roots=["."],
                verification_commands=_verification_command(),
            )
        )

    def test_effective_policy_view_exposes_hardening(self) -> None:
        from local_dev_mcp.introspection import effective_policy_view

        bare = ProjectPolicy.model_validate(self._base(self.base))
        view = effective_policy_view(bare, (Path("/nonexistent"),))
        self.assertIsNone(view["filesystem_policy"])
        self.assertIsNone(view["credential_policy"])
        self.assertIsNone(view["sensitivity_policy"])
        self.assertFalse(view["signed_manifests"])

        hardened = ProjectPolicy.model_validate(
            self._base(
                self.base,
                capabilities=_export_capabilities(),
                commit_roots=["."],
                verification_commands=_verification_command(),
                filesystem_policy={"read_allow": ["docs"]},
                credential_policy={"allow": ["GH_TOKEN"]},
                sensitivity_policy={"sensitive_paths": ["config/secrets/**"]},
                signed_manifests=True,
            )
        )
        view = effective_policy_view(hardened, (Path("/nonexistent"),))
        self.assertEqual(view["filesystem_policy"]["read_allow"], ["docs"])
        self.assertEqual(view["credential_policy"]["allow"], ["GH_TOKEN"])
        self.assertTrue(view["signed_manifests"])
        self.assertEqual(
            view["sensitivity_policy"]["sensitive_paths"], ["config/secrets/**"]
        )


class FileSystemPolicyTests(unittest.TestCase):
    """Read/write allowlists are enforced in the reader and writer surfaces."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = _repo(self.base)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
        (self.root / "docs" / "nested").mkdir()
        (self.root / "docs" / "nested" / "deep.md").write_text(
            "deep", encoding="utf-8"
        )
        (self.root / "src" / "notes.md").write_text("notes", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_read_allowlist_permits_allowed_paths_and_blocks_others(self) -> None:
        project = ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test Project",
                "description": "Reader policy tests",
                "root": str(self.root),
                "capabilities": ["repository_read"],
                "filesystem_policy": {"read_allow": ["docs"]},
            }
        )
        reader = RepositoryReader(
            Settings.from_env(
                {"PROJECTS_CONFIG_PATH": str(self.base / "projects.json")}
            )
        )
        result = asyncio.run(reader.read_markdown(project, "docs/guide.md"))
        self.assertEqual(result["content"], "# Guide\n")
        # A ``**`` subtree entry also permits nested files.
        with self.assertRaises(RepositoryReadError):
            asyncio.run(reader.read_markdown(project, "src/notes.md"))
        listing = asyncio.run(reader.list_markdown(project, ".", max_depth=2))
        listed = {entry["path"] for entry in listing["files"]}
        self.assertIn("docs/guide.md", listed)
        self.assertNotIn("src/notes.md", listed)

    def test_write_allowlist_intersects_with_approved_write_roots(self) -> None:
        project = writer_policy(
            self.root,
            filesystem_policy={"write_allow": ["docs"]},
        )
        writer = RepositoryWriter(
            Settings.from_env(
                {"PROJECTS_CONFIG_PATH": str(self.base / "projects.json")}
            )
        )
        # docs is in both markdown_write_roots and the allowlist: allowed.
        result = asyncio.run(
            writer.write(project, "docs/new.md", "# New\n", write_mode="create")
        )
        self.assertEqual(result["write_mode"], "create")
        # canonical is an approved write root but NOT in the write allowlist.
        with self.assertRaises(RepositoryWriteError) as raised:
            asyncio.run(
                writer.write(project, "canonical/x.md", "# X\n", write_mode="create")
            )
        self.assertIn("filesystem write allowlist", str(raised.exception))

    def test_allowlist_matching_is_prefix_based(self) -> None:
        project = ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test Project",
                "description": "Allowlist matching tests",
                "root": str(self.root),
                "capabilities": ["repository_read"],
                "filesystem_policy": {"read_allow": ["docs"]},
            }
        )
        # ``docs`` must not accidentally allow ``docs2``.
        self.assertTrue(filesystem_read_allowed(project, "docs/guide.md"))
        self.assertTrue(filesystem_read_allowed(project, "docs/nested/deep.md"))
        self.assertFalse(filesystem_read_allowed(project, "docs2/guide.md"))
        self.assertFalse(filesystem_read_allowed(project, "src/notes.md"))
        # An unconfigured project is always allowed.
        bare = ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test Project",
                "description": "Bare",
                "root": str(self.root),
                "capabilities": ["repository_read"],
            }
        )
        self.assertTrue(filesystem_read_allowed(bare, "src/notes.md"))
        self.assertTrue(filesystem_write_allowed(bare, "anything"))


class CredentialProjectionTests(unittest.TestCase):
    """credential_policy projects ONLY declared names into worktree subprocesses."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = _repo(self.base)
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "5",
                "MAX_VERIFICATION_OUTPUT_BYTES": "256",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projected_credential_env_only_copies_declared_names(self) -> None:
        with patch.dict(os.environ, {"GH_TOKEN": "secret", "OTHER_SECRET": "x"}, clear=False):
            policy = CredentialPolicy(allow=("GH_TOKEN", "MISSING_VAR"))
            env = projected_credential_env(policy, base={"PATH": "/usr/bin"})
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["GH_TOKEN"], "secret")
            self.assertNotIn("MISSING_VAR", env)
            self.assertNotIn("OTHER_SECRET", env)
            # Unset policy: base is returned unchanged (no projection).
            self.assertEqual(
                projected_credential_env(None, base={"A": "1"}), {"A": "1"}
            )

    def test_supply_chain_scanner_receives_only_projected_credentials(self) -> None:
        # The scanner only passes when GH_TOKEN is present with the exact value.
        scanner = [
            "{python}",
            "-c",
            "import os, sys; "
            "sys.exit(0 if os.environ.get('GH_TOKEN') == 'secret' else 7)",
        ]
        command = ["{python}", "-c", "import sys; sys.exit(0)"]

        async def run_commit(policy: ProjectPolicy) -> dict[str, Any]:
            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings,
                locks,
                EditingExecutor(
                    lambda root: (root / "README.md").write_text(
                        "# Candidate\n", encoding="utf-8"
                    )
                ),  # type: ignore[arg-type]
                projects=(policy,),
            )
            manager = VerificationCommitManager(
                self.settings, locks, jobs, projects=(policy,)
            )
            started = await jobs.start(policy, "make candidate", "high")
            completed = await wait_for_status(
                jobs, policy, str(started["job_id"]), {"completed"}
            )
            verified = await manager.verify(
                policy, str(completed["job_id"]), str(completed["base_head"])
            )
            self.assertEqual(verified["status"], "passed", verified)
            return await manager.commit(
                policy,
                str(completed["job_id"]),
                str(verified["verification_id"]),
                str(verified["base_head"]),
                str(verified["diff_fingerprint"]),
                list(verified["paths"]),
                "M-CC-11 credential projection candidate",
            )

        async def exercise() -> None:
            base_policy = {
                "id": "test-project",
                "display_name": "Test Project",
                "description": "Credential projection tests",
                "root": str(self.root),
                "capabilities": _verify_capabilities(),
                "commit_roots": ["."],
                "verification_commands": [
                    {"id": "core", "argv": command, "python_path": ["src"]}
                ],
                "supply_chain_scans": [{"id": "gha", "argv": scanner}],
                "protected_paths": [".git", ".env"],
            }
            # Projected: the declared GH_TOKEN reaches the scanner in the
            # worktree, so the gate passes and the commit lands.
            with patch.dict(os.environ, {"GH_TOKEN": "secret"}):
                projected = ProjectPolicy.model_validate(
                    {**base_policy, "credential_policy": {"allow": ["GH_TOKEN"]}}
                )
                committed = await run_commit(projected)
                self.assertEqual(committed["status"], "committed")

            # Least privilege: with no credential_policy, the bridge's GH_TOKEN
            # must NOT leak into the worktree scanner, so the gate fails closed
            # and the commit is rejected.
            with patch.dict(os.environ, {"GH_TOKEN": "secret"}):
                unprojected = ProjectPolicy.model_validate(base_policy)
                with self.assertRaises(VerificationError):
                    await run_commit(unprojected)

        asyncio.run(exercise())


class SensitivityClassificationTests(unittest.TestCase):
    """sensitivity_policy redacts/excludes declared artifacts before export."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = _repo(self.base)
        self.settings = evidence_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def prepare(
        self, policy: ProjectPolicy
    ) -> tuple[VerificationCommitManager, EvidenceBundleManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(
                lambda root: (root / "README.md").write_text(
                    "# Candidate\n", encoding="utf-8"
                )
            ),  # type: ignore[arg-type]
            projects=(policy,),
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        evidence = EvidenceBundleManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_for_status(
            jobs, policy, str(started["job_id"]), {"completed"}
        )
        verified = await verification.verify(
            policy, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        return verification, evidence, verified

    def test_sensitive_artifact_redacted_from_bundle_with_correct_count(self) -> None:
        async def exercise() -> None:
            policy = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test Project",
                    "description": "Sensitivity classification tests",
                    "root": str(self.root),
                    "capabilities": _export_capabilities(),
                    "commit_roots": ["."],
                    "verification_commands": [
                        {
                            "id": "core",
                            "argv": ["{python}", "-c", "print('ok')"],
                            "python_path": ["src"],
                        }
                    ],
                    "supply_chain_scans": [],
                    "protected_paths": [".git", ".env"],
                    "sensitivity_policy": {
                        "sensitive_paths": ["config/internal/**"]
                    },
                }
            )
            _, evidence, verified = await self.prepare(policy)
            job_id = str(verified["job_id"])
            existing = store.load_latest_checkpoint(evidence._db, job_id)
            next_seq = (existing["seq"] + 1) if existing else 1
            seeded_paths = [
                "src/safe.py",
                "config/internal/roadmap.md",
                "config/internal/plan.md",
                "docs/README.md",
            ]
            store.insert_checkpoint_row(
                evidence._db,
                {
                    "job_id": job_id,
                    "project_id": policy.id,
                    "kind": "test-injected",
                    "seq": next_seq,
                    "recorded_at": "2026-08-27T00:00:00Z",
                    "changed_paths_json": json.dumps(seeded_paths),
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
            # Two of the four seeded paths fall under the declared sensitive
            # prefix; both must be dropped and counted.
            self.assertEqual(candidate["sensitive_excluded_count"], 2)
            self.assertEqual(
                set(candidate["changed_paths"]), {"src/safe.py", "docs/README.md"}
            )
            # The sensitivity section records the policy and the exclusion count.
            self.assertEqual(
                bundle["sensitivity"]["sensitive_paths"],
                ["config/internal/**"],
            )
            self.assertEqual(bundle["sensitivity"]["excluded_count"], 2)
            # The sensitive path strings never appear anywhere in the bundle.
            bundle_text = json.dumps(bundle)
            self.assertNotIn("config/internal/roadmap.md", bundle_text)
            self.assertNotIn("config/internal/plan.md", bundle_text)
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

    def test_exclude_sensitive_paths_extends_with_project_prefixes(self) -> None:
        prefixes = project_sensitivity_prefixes(
            ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test Project",
                    "description": "Sensitivity prefix tests",
                    "root": str(self.root),
                    "capabilities": _export_capabilities(),
                    "commit_roots": ["."],
                    "verification_commands": _verification_command(),
                    "sensitivity_policy": {
                        "sensitive_paths": ["config/secrets/**"]
                    },
                }
            ).sensitivity_policy
        )
        self.assertEqual(prefixes, ("config/secrets",))
        paths = [
            "src/safe.py",
            "config/secrets/token.txt",
            "config/secrets/nested/key.md",
            "README.md",
        ]
        kept, excluded = _exclude_sensitive_paths(paths, sensitive_prefixes=prefixes)
        self.assertEqual(kept, ["src/safe.py", "README.md"])
        self.assertEqual(excluded, 2)
        detail = (
            " src/safe.py                 | 1 +\n"
            " config/secrets/token.txt    | 4 ++\n"
            " README.md                   | 1 +\n"
        )
        scrubbed = _scrub_sensitive_diff_detail(detail, sensitive_prefixes=prefixes)
        # The sensitive line is replaced with a marker; the path never appears.
        self.assertNotIn("config/secrets/token.txt", scrubbed)
        self.assertEqual(scrubbed.count("<excluded-sensitive>"), 1)
        self.assertIn("src/safe.py", scrubbed)


class SignedManifestTests(unittest.TestCase):
    """signed_manifests signs the bundle and stores the manifest alongside it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = _repo(self.base)
        self.settings = evidence_settings(self.base)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signed_policy(self) -> ProjectPolicy:
        return ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test Project",
                "description": "Signed manifest tests",
                "root": str(self.root),
                "capabilities": _export_capabilities(),
                "commit_roots": ["."],
                "verification_commands": [
                    {
                        "id": "core",
                        "argv": ["{python}", "-c", "print('ok')"],
                        "python_path": ["src"],
                    }
                ],
                "protected_paths": [".git", ".env"],
                "signed_manifests": True,
            }
        )

    async def export_signed_bundle(
        self, policy: ProjectPolicy, key: bytes
    ) -> tuple[EvidenceBundleManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(
                lambda root: (root / "README.md").write_text(
                    "# Candidate\n", encoding="utf-8"
                )
            ),  # type: ignore[arg-type]
            projects=(policy,),
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        evidence = EvidenceBundleManager(
            self.settings, locks, jobs, projects=(policy,)
        )
        started = await jobs.start(policy, "make candidate", "high")
        completed = await wait_for_status(
            jobs, policy, str(started["job_id"]), {"completed"}
        )
        verified = await verification.verify(
            policy, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        bundle = await evidence.export_bundle(
            policy,
            str(completed["job_id"]),
            str(verified["diff_fingerprint"]),
        )
        return evidence, bundle

    def test_signed_manifest_attached_verifies_and_is_stored(self) -> None:
        async def exercise() -> None:
            key = b"test-signing-key-0123456789abcdef"
            with patch.dict(
                os.environ, {"BRIDGELAB_EVIDENCE_SIGNING_KEY": key.decode()}
            ):
                policy = self._signed_policy()
                evidence, bundle = await self.export_signed_bundle(policy, key)

            manifest = bundle["manifest"]
            self.assertEqual(manifest["algorithm"], "hmac-sha256")
            self.assertEqual(manifest["project_id"], policy.id)
            self.assertEqual(manifest["bundle_sha256"], bundle["bundle_sha256"])
            self.assertTrue(len(manifest["signature"]) == 64)
            # The manifest verifies with the right key and fails with a wrong key.
            self.assertTrue(verify_evidence_signature(manifest, key))
            self.assertFalse(verify_evidence_signature(manifest, b"wrong-key"))

            # The manifest was persisted alongside the bundle in the state DB.
            job_id = bundle["job_id"]
            rows = store.load_evidence_manifests(evidence._db, job_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["project_id"], policy.id)
            self.assertEqual(rows[0]["bundle_sha256"], bundle["bundle_sha256"])
            self.assertEqual(rows[0]["signature"], manifest["signature"])

            # The content address is recomputable by stripping both the digest
            # and the manifest (the manifest covers the digest).
            body = json.dumps(
                {
                    k: v
                    for k, v in bundle.items()
                    if k not in ("bundle_sha256", "manifest")
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            self.assertEqual(
                bundle["bundle_sha256"],
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )

        asyncio.run(exercise())

    def test_sign_verify_roundtrip_unit(self) -> None:
        bundle = {"project_id": "test-project", "bundle_sha256": "ab" * 32}
        key = b"key" * 16
        signed = sign_evidence_bundle(bundle, "test-project", key)
        self.assertTrue(verify_evidence_signature(signed["manifest"], key))
        # Tampering with the digest or project id invalidates the signature.
        tampered = dict(signed["manifest"])
        tampered["bundle_sha256"] = "cd" * 32
        self.assertFalse(verify_evidence_signature(tampered, key))
        self.assertFalse(verify_evidence_signature({"algorithm": "nope"}, key))

    def test_signed_manifests_fails_closed_without_key(self) -> None:
        async def exercise() -> None:
            policy = self._signed_policy()
            with patch.dict(os.environ, {}, clear=False) as env:
                for name in (
                    "BRIDGELAB_EVIDENCE_SIGNING_KEY",
                    "BRIDGELAB_EVIDENCE_SIGNING_KEY_TEST_PROJECT",
                ):
                    env.pop(name, None)
                with self.assertRaises(EvidenceBundleError):
                    await self.export_signed_bundle(policy, b"unused")

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
