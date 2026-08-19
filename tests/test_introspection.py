"""Tests for the M-AL-01 read-only introspection surface (M-AL-01).

Covers the three dormant-by-default introspection tools and the
``CanonicalizationResolver``'s four-layer evidence model:

* ``effective_policy_view`` — deterministic, redacted policy view + fingerprint.
* ``DevelopmentJobManager.list_jobs`` / ``committed_job_summary`` — sanitized,
  bounded job enumeration and the commit->job lookup the resolver depends on.
* ``CanonicalizationResolver.resolve`` — twenty-one hermetic reconciliation
  fixtures spanning the CANONICAL / NOT_CANONICAL / INDETERMINATE verdict matrix,
  the structured ``canonicalization_method``, the distinct ``landing_is_ancestor``
  fact, per-path content equivalence at landing, the INDETERMINATE edge cases
  (unreadable canonical tip, gh outage, unknown/unavailable landing SHA, transient
  content-read failure), plus a security fixture proving the resolver never
  fetches, pushes, merges, tags, or moves a ref.

The reconciliation fixtures reuse the hermetic bare-origin pattern from
``test_sync`` (its ``add_origin`` sets the ``origin/HEAD`` symref the resolver
reads) and drive a real job through verify -> commit -> publish so the detached
commit, verification record, and publication record are genuine. ``gh`` is
mocked (no real GitHub) for the pull-request layers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fastmcp.exceptions import ToolError

from local_dev_mcp import git as git_module
from local_dev_mcp import introspection as introspection_module
from local_dev_mcp import store
from local_dev_mcp import verification as verification_module
from local_dev_mcp.config import Settings
from local_dev_mcp.introspection import CanonicalizationResolver, effective_policy_view
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.pr import PullRequestError, PullRequestManager
from local_dev_mcp.registry import (
    ProjectLockManager,
    ProjectRegistry,
    ProjectRegistryError,
)
from local_dev_mcp.server import create_server
from local_dev_mcp.sync import SyncManager
from local_dev_mcp.verification import (
    PublicationManager,
    VerificationCommitManager,
)

from tests.test_sync import add_origin, init_repo, rev
from tests.test_verification import EditingExecutor, wait_completed


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    """Policy that can run jobs, publish, and introspect (M-AL-01)."""
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Introspection tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "claude_edit",
            "verification_commit",
            "push_development_commit",
            "job_introspection",
            "policy_introspection",
            "canonical_history_read",
        ],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": ["{python}", "-c", "pass"]},
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


# Read-only git subcommands the canonicalization resolver is permitted to issue.
# Anything outside this set (fetch, push, pull, merge, rebase, tag, update-ref,
# reset, checkout, commit, rm, clean, stash) would violate the read-only invariant.
_READ_ONLY_GIT = {
    "rev-parse",
    "merge-base",
    "log",
    "ls-tree",
    "cat-file",
    "ls-remote",
    "symbolic-ref",
    "show",
    "diff",
    "status",
    "for-each-ref",
    "var",
}


class ReconcileFixture(unittest.TestCase):
    """Shared hermetic bare-origin fixture for the reconciliation tests."""

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

    def _build(
        self, project_obj: ProjectPolicy, edit: Any
    ) -> tuple[
        DevelopmentJobManager,
        VerificationCommitManager,
        PublicationManager,
        PullRequestManager,
        SyncManager,
        CanonicalizationResolver,
    ]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings, locks, EditingExecutor(edit), projects=(project_obj,)
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(project_obj,)
        )
        publication = PublicationManager(
            self.settings, locks, jobs, projects=(project_obj,)
        )
        pr = PullRequestManager(self.settings, locks, jobs, projects=(project_obj,))
        sync = SyncManager(self.settings, locks, jobs, projects=(project_obj,))
        canonicalization = CanonicalizationResolver(
            self.settings, locks, jobs, pr, sync, projects=(project_obj,)
        )
        return jobs, verification, publication, pr, sync, canonicalization

    async def _pipeline(
        self,
        project_obj: ProjectPolicy,
        edit: Any,
        branch: str,
    ) -> tuple[
        DevelopmentJobManager,
        VerificationCommitManager,
        PublicationManager,
        PullRequestManager,
        SyncManager,
        CanonicalizationResolver,
        dict[str, Any],
    ]:
        jobs, verification, publication, pr, sync, canonicalization = self._build(
            project_obj, edit
        )
        started = await jobs.start(project_obj, "make candidate", "high")
        completed = await wait_completed(jobs, project_obj, str(started["job_id"]))
        self.assertEqual(completed["status"], "completed")
        verified = await verification.verify(
            project_obj, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        committed = await verification.commit(
            project_obj,
            str(completed["job_id"]),
            str(verified["verification_id"]),
            str(verified["base_head"]),
            str(verified["diff_fingerprint"]),
            list(verified["paths"]),
            "MCPB introspection candidate",
        )
        self.assertEqual(committed["status"], "committed")
        published = await publication.publish(
            project_obj,
            str(completed["job_id"]),
            committed["commit_sha"],
            branch,
            str(verified["base_head"]),
            str(verified["diff_fingerprint"]),
        )
        self.assertEqual(published["status"], "published")
        evidence = {
            "job_id": str(completed["job_id"]),
            "commit_sha": committed["commit_sha"],
            "base_head": str(verified["base_head"]),
            "fingerprint": str(verified["diff_fingerprint"]),
            "verification_id": str(verified["verification_id"]),
            "branch": branch,
        }
        return jobs, verification, publication, pr, sync, canonicalization, evidence

    async def _resolve(
        self,
        canonicalization: CanonicalizationResolver,
        project_obj: ProjectPolicy,
        commit_sha: str,
        gh_side_effect: tuple[str, ...] = ("",),
    ) -> dict[str, Any]:
        gh_mock = mock.AsyncMock(side_effect=list(gh_side_effect))
        with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
            return await canonicalization.resolve(project_obj, commit_sha)

    def _fast_forward_origin_to_commit(self, commit_sha: str, branch: str) -> None:
        """Simulate a merged PR by fast-forwarding origin/main to the commit."""
        clone = self.base / "ff-clone"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(clone)], check=True)
        self._config(clone)
        subprocess.run(
            ["git", "-C", str(clone), "fetch", "-q", "origin", f"refs/heads/{branch}"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "merge", "--ff-only", "FETCH_HEAD"], check=True
        )
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)

    def _squash_origin_to_commit_tree(self, commit_sha: str, branch: str) -> str:
        """Simulate a squash merge: a NEW commit on main with the commit's tree."""
        clone = self.base / "sq-clone"
        subprocess.run(["git", "clone", "-q", str(self.bare), str(clone)], check=True)
        self._config(clone)
        subprocess.run(
            ["git", "-C", str(clone), "fetch", "-q", "origin", f"refs/heads/{branch}"],
            check=True,
        )
        # Reset index + working tree to the published commit's tree while leaving
        # HEAD on main (base); the new commit's parent is base, its tree is the
        # published tree -> a content-equivalent squash, not an ancestor.
        subprocess.run(
            ["git", "-C", str(clone), "read-tree", "-u", "--reset", commit_sha],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(clone), "commit", "-qm", "Squash merge"], check=True
        )
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)
        return rev(self.bare, "main")

    def _sync_root(self) -> None:
        """Populate the canonical checkout's object store from origin (fixture
        setup only; the resolver itself never fetches)."""
        subprocess.run(["git", "-C", str(self.root), "fetch", "-q", "origin"], check=True)

    def _config(self, clone: Path) -> None:
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.name", "Test User"], check=True
        )
        subprocess.run(
            ["git", "-C", str(clone), "config", "user.email", "test@example.invalid"],
            check=True,
        )

    def _delete_origin_head_symref(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
            check=True,
        )

    def _for_each_ref(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), "for-each-ref", "--format=%(refname) %(objectname)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout


# ---------------------------------------------------------------------------
# effective_policy_view
# ---------------------------------------------------------------------------


class EffectivePolicyViewTests(unittest.TestCase):
    def test_view_is_deterministic_redacted_with_stable_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            init_repo(root)
            project_obj = policy(root)
            paths = (root, Path(directory))
            view = effective_policy_view(project_obj, paths)

            self.assertTrue(view["root_redacted"])
            self.assertEqual(view["project_id"], "test-project")
            self.assertEqual(view["capabilities"], sorted(view["capabilities"]))
            self.assertEqual(view["markdown_write_roots"], sorted(view["markdown_write_roots"]))
            self.assertEqual(view["commit_roots"], sorted(view["commit_roots"]))
            self.assertEqual(view["protected_paths"], sorted(view["protected_paths"]))
            self.assertRegex(view["policy_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertNotIn("root", view)
            self.assertIn("verification_commands", view)
            # Determinism: same policy -> same fingerprint.
            self.assertEqual(
                effective_policy_view(project_obj, paths)["policy_fingerprint"],
                view["policy_fingerprint"],
            )
            # A capability change flips the fingerprint.
            changed = policy(root, capabilities=policy(root).capabilities)
            self.assertEqual(
                effective_policy_view(changed, paths)["policy_fingerprint"],
                view["policy_fingerprint"],
            )
            fewer = ProjectPolicy.model_validate(
                {**project_obj.model_dump(mode="json"), "capabilities": ["repository_read"]}
            )
            self.assertNotEqual(
                effective_policy_view(fewer, paths)["policy_fingerprint"],
                view["policy_fingerprint"],
            )

    def test_redacts_embedded_absolute_paths_in_verification_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            init_repo(root)
            lab_root = Path(directory) / "agent-lab"
            lab_root.mkdir()
            project_obj = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test",
                    "description": "Redaction test",
                    "root": str(root),
                    "capabilities": ["repository_read", "policy_introspection"],
                    "verification_commands": [
                        {
                            "id": "lab-doctor",
                            "argv": ["env", f"LAB_ROOT={lab_root}", "./bin/lab", "doctor"],
                        }
                    ],
                    "commit_roots": ["."],
                    "protected_paths": [".git", ".env"],
                }
            )
            view = effective_policy_view(project_obj, (root, lab_root))
            serialized = json.dumps(view, default=str)
            self.assertNotIn(str(lab_root), serialized)
            self.assertNotIn(str(root), serialized)
            self.assertIn("<redacted-path>", serialized)
            argv = view["verification_commands"][0]["argv"]
            self.assertTrue(any("redacted-path" in str(a) for a in argv))


# ---------------------------------------------------------------------------
# list_development_jobs + committed_job_summary
# ---------------------------------------------------------------------------


class ListDevelopmentJobsTests(ReconcileFixture):
    async def _two_jobs(
        self,
    ) -> tuple[ProjectPolicy, DevelopmentJobManager, dict[str, Any], dict[str, Any]]:
        project_obj = policy(self.root)

        async def run_one(edit: Any, branch: str | None) -> dict[str, Any]:
            jobs, verification, publication, *_ = self._build(project_obj, edit)
            started = await jobs.start(project_obj, "make candidate", "high")
            completed = await wait_completed(jobs, project_obj, str(started["job_id"]))
            verified = await verification.verify(
                project_obj, str(completed["job_id"]), str(completed["base_head"])
            )
            committed = await verification.commit(
                project_obj,
                str(completed["job_id"]),
                str(verified["verification_id"]),
                str(verified["base_head"]),
                str(verified["diff_fingerprint"]),
                list(verified["paths"]),
                "candidate",
            )
            evidence = {
                "job_id": str(completed["job_id"]),
                "commit_sha": committed["commit_sha"],
                "base_head": str(verified["base_head"]),
            }
            if branch is not None:
                await publication.publish(
                    project_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    branch,
                    evidence["base_head"],
                    str(verified["diff_fingerprint"]),
                )
            return evidence

        # Job A: published. Job B: committed only (no publication).
        a = await run_one(
            lambda root: (root / "candidate.txt").write_text("a\n", encoding="utf-8"),
            "agent/job-a",
        )
        b = await run_one(
            lambda root: (root / "other.txt").write_text("b\n", encoding="utf-8"),
            None,
        )
        # Rebuild a fresh manager over the persisted store so the in-memory + durable
        # views agree (the two jobs ran under two separate managers above).
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings, locks, EditingExecutor(lambda r: None), projects=(project_obj,)
        )
        return project_obj, jobs, a, b

    def test_returns_sanitized_entries_with_governance_index(self) -> None:
        async def exercise() -> None:
            project_obj, jobs, a, b = await self._two_jobs()
            result = await jobs.list_jobs(project_obj, "all", 50)

            self.assertEqual(result["project_id"], "test-project")
            self.assertEqual(result["scope"], "all")
            self.assertEqual(result["count"], 2)
            self.assertFalse(result["truncated"])
            self.assertEqual(result["limit_applied"], 50)
            self.assertEqual(len(result["jobs"]), 2)
            forbidden = {
                "session_id",
                "worktree_path",
                "result",
                "candidate",
                "error",
                "cancel_requested",
            }
            for entry in result["jobs"]:
                self.assertEqual(set(entry) & forbidden, set())
                self.assertIn(entry["job_id"], {a["job_id"], b["job_id"]})
                self.assertEqual(entry["project_id"], "test-project")
                self.assertEqual(entry["execution_phase"], "terminal")
                self.assertTrue(entry["committed"])
                self.assertTrue(entry["verification_present"])
                self.assertIn("committed_sha", entry)
                self.assertIn("base_head", entry)
            published = next(e for e in result["jobs"] if e["job_id"] == a["job_id"])
            self.assertTrue(published["publication_present"])
            self.assertEqual(published["publication_branch"], "agent/job-a")
            self.assertEqual(published["committed_sha"], a["commit_sha"])
            committed_only = next(e for e in result["jobs"] if e["job_id"] == b["job_id"])
            self.assertFalse(committed_only["publication_present"])
            self.assertNotIn("publication_branch", committed_only)
            # No absolute path leaks into the serialized envelope.
            serialized = json.dumps(result, default=str)
            self.assertNotIn(str(self.base), serialized)
            self.assertNotIn(str(self.settings.job_worktree_root), serialized)

        asyncio.run(exercise())

    def test_scope_and_limit_clamping(self) -> None:
        async def exercise() -> None:
            project_obj, jobs, _a, _b = await self._two_jobs()
            self.assertEqual((await jobs.list_jobs(project_obj, "terminal", 50))["count"], 2)
            self.assertEqual((await jobs.list_jobs(project_obj, "active", 50))["count"], 0)
            # limit below the count -> truncated, count == limit.
            one = await jobs.list_jobs(project_obj, "all", 1)
            self.assertEqual(one["count"], 1)
            self.assertTrue(one["truncated"])
            self.assertEqual(one["limit_applied"], 1)
            # limit 0 -> empty page; truncated because 2 jobs exist beyond it.
            zero = await jobs.list_jobs(project_obj, "all", 0)
            self.assertEqual(zero["count"], 0)
            self.assertTrue(zero["truncated"])
            self.assertEqual(zero["limit_applied"], 0)
            # limit above the hard cap is clamped to MAX_LIST_JOBS (200).
            big = await jobs.list_jobs(project_obj, "all", 999)
            self.assertEqual(big["limit_applied"], 200)
            # Invalid scope / negative limit are rejected.
            with self.assertRaises(Exception):
                await jobs.list_jobs(project_obj, "bogus", 10)
            with self.assertRaises(Exception):
                await jobs.list_jobs(project_obj, "all", -1)

        asyncio.run(exercise())

    def test_committed_job_summary_returns_safe_fields_only(self) -> None:
        async def exercise() -> None:
            project_obj, jobs, a, _b = await self._two_jobs()
            summary = await jobs.committed_job_summary(project_obj.id, a["commit_sha"])
            assert summary is not None
            self.assertEqual(summary["committed_sha"], a["commit_sha"])
            self.assertEqual(set(summary), {"job_id", "status", "base_head", "committed_sha"})
            # Unknown commit -> None.
            self.assertIsNone(
                await jobs.committed_job_summary(project_obj.id, "1" * 40)
            )

        asyncio.run(exercise())


# ---------------------------------------------------------------------------
# Authorization (capability gates)
# ---------------------------------------------------------------------------


class AuthorizationTests(unittest.TestCase):
    def _server(self, capabilities: list[str]) -> Any:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            init_repo(root)
            project_obj = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test",
                    "description": "Auth",
                    "root": str(root),
                    "capabilities": capabilities,
                    "commit_roots": ["."],
                    "verification_commands": [
                        {"id": "core", "argv": ["{python}", "-c", "pass"]}
                    ],
                    "protected_paths": [".git", ".env"],
                }
            )
            server = create_server(
                Settings.from_env(
                    {
                        "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                        "JOB_WORKTREE_ROOT": str(base / "jobs"),
                    }
                ),
                ProjectRegistry({"test-project": project_obj}),
            )
            return server

    def test_introspection_capabilities_are_gated(self) -> None:
        async def exercise() -> None:
            # Project grants only repository_read: none of the three introspection
            # capabilities resolve.
            registry = ProjectRegistry(
                {
                    "test-project": ProjectPolicy.model_validate(
                        {
                            "id": "test-project",
                            "display_name": "T",
                            "description": "T",
                            "root": str(Path(tempfile.mkdtemp()) / "r"),
                            "capabilities": ["repository_read"],
                        }
                    )
                }
            )
            for capability in (
                ProjectCapability.JOB_INTROSPECTION,
                ProjectCapability.POLICY_INTROSPECTION,
                ProjectCapability.CANONICAL_HISTORY_READ,
            ):
                with self.assertRaises(ProjectRegistryError):
                    registry.resolve("test-project", capability)

            # A project granting the prerequisite but not the introspection
            # capability is still rejected for the introspection capability.
            with_claude = ProjectRegistry(
                {
                    "test-project": ProjectPolicy.model_validate(
                        {
                            "id": "test-project",
                            "display_name": "T",
                            "description": "T",
                            "root": str(Path(tempfile.mkdtemp()) / "r"),
                            "capabilities": ["repository_read", "claude_edit"],
                        }
                    )
                }
            )
            with self.assertRaises(ProjectRegistryError):
                with_claude.resolve("test-project", ProjectCapability.JOB_INTROSPECTION)

        asyncio.run(exercise())

    def test_list_development_jobs_requires_capability_via_server(self) -> None:
        async def exercise() -> None:
            server = self._server(["repository_read", "claude_edit", "verification_commit"])
            with self.assertRaises(ToolError):
                await server.call_tool(
                    "list_development_jobs", {"project_id": "test-project"}
                )

        asyncio.run(exercise())

    def test_resolve_canonicalization_requires_capability_via_server(self) -> None:
        async def exercise() -> None:
            server = self._server(["repository_read", "claude_edit", "verification_commit"])
            with self.assertRaises(ToolError):
                await server.call_tool(
                    "resolve_candidate_canonicalization",
                    {"project_id": "test-project", "commit_sha": "0" * 40},
                )

        asyncio.run(exercise())


# ---------------------------------------------------------------------------
# resolve_candidate_canonicalization — 21 hermetic reconciliation fixtures
# ---------------------------------------------------------------------------


class CanonicalizationResolverTests(ReconcileFixture):
    """Twenty-one fixtures covering the verdict matrix + the read-only invariant."""

    # 1. CANONICAL via direct ancestry (origin fast-forwarded to the commit).
    def test_canonical_via_direct_ancestry(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("c\n", encoding="utf-8"),
                "agent/canonical-1",
            )
            self._fast_forward_origin_to_commit(evidence["commit_sha"], evidence["branch"])
            self._sync_root()
            result = await self._resolve(canonicalization, project_obj, evidence["commit_sha"])
            self.assertEqual(result["verdict"], "CANONICAL")
            self.assertTrue(result["canonical"]["candidate_is_ancestor"])
            self.assertFalse(result["canonical"]["content_equivalent"])
            self.assertEqual(result["canonical"]["current_head"], evidence["commit_sha"])
            self.assertEqual(result["canonicalization_method"], "exact_commit_ancestor")
            self.assertTrue(result["provenance"]["job_present"])
            self.assertEqual(result["content_check"]["status"], "matched")

        asyncio.run(exercise())

    # 2. CANONICAL via squash/rebase content-equivalence (new commit, same tree).
    def test_canonical_via_squash_content_equivalence(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("squash\n", encoding="utf-8"),
                "agent/canonical-2",
            )
            tip = self._squash_origin_to_commit_tree(evidence["commit_sha"], evidence["branch"])
            self._sync_root()
            result = await self._resolve(canonicalization, project_obj, evidence["commit_sha"])
            self.assertEqual(result["verdict"], "CANONICAL")
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertTrue(result["canonical"]["content_equivalent"])
            self.assertEqual(result["canonical"]["current_head"], tip)
            self.assertEqual(result["canonical"]["matched_canonical_commit"], tip)
            self.assertEqual(result["canonicalization_method"], "merged_pr_content_equivalent")
            # The squash landing is tree-identical, so the per-path content
            # equivalence check (run against the matched canonical commit) passes.
            self.assertEqual(result["content_equivalence"]["status"], "equivalent")
            self.assertEqual(result["content_equivalence"]["mismatches"], [])
            self.assertEqual(result["content_equivalence"]["landing_commit"], tip)
            self.assertTrue(result["provenance"]["publication_present"])

        asyncio.run(exercise())

    # 3. NOT_CANONICAL: published but not yet absorbed into canonical history.
    def test_not_canonical_published_not_merged(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("n\n", encoding="utf-8"),
                "agent/notcanon-3",
            )
            result = await self._resolve(canonicalization, project_obj, evidence["commit_sha"])
            self.assertEqual(result["verdict"], "NOT_CANONICAL")
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertFalse(result["canonical"]["content_equivalent"])
            self.assertEqual(result["canonical"]["default_branch"], "main")
            self.assertEqual(result["canonical"]["current_head"], self.base_head)
            self.assertTrue(result["provenance"]["job_present"])
            self.assertTrue(result["provenance"]["publication_present"])
            self.assertEqual(result["provenance"]["publication_branch"], "agent/notcanon-3")
            self.assertEqual(result["canonicalization_method"], "not_merged")
            # No landing commit is available (no PR, no tree match), so the
            # landing content-equivalence check is intentionally not run.
            self.assertEqual(result["content_equivalence"]["status"], "not_checked")
            self.assertEqual(result["content_check"]["status"], "matched")

        asyncio.run(exercise())

    # 4. NOT_CANONICAL with an open pull request (gh mocked).
    def test_not_canonical_with_open_pr(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("pr\n", encoding="utf-8"),
                "agent/pr-4",
            )
            branch = evidence["branch"]
            discover = json.dumps(
                [{"number": 7, "url": "https://example/repo/pull/7", "state": "OPEN",
                  "headRefName": branch}]
            )
            view = json.dumps(
                {"number": 7, "state": "OPEN", "merged": False, "mergeCommit": None,
                 "baseRefName": "main", "headRefName": branch,
                 "url": "https://example/repo/pull/7"}
            )
            result = await self._resolve(
                canonicalization, project_obj, evidence["commit_sha"], (discover, view)
            )
            self.assertEqual(result["verdict"], "NOT_CANONICAL")
            self.assertIsNotNone(result["pull_request"])
            self.assertTrue(result["pull_request"]["present"])
            self.assertEqual(result["pull_request"]["number"], 7)
            self.assertFalse(result["pull_request"]["merged"])
            self.assertIsNone(result["pull_request"]["merge_commit_sha"])
            self.assertTrue(result["remote_branch"]["present"])
            self.assertEqual(result["remote_branch"]["head_sha"], evidence["commit_sha"])

        asyncio.run(exercise())

    # 5. CANONICAL with a merged pull request (ancestry + gh view merged).
    def test_canonical_with_merged_pr(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("m\n", encoding="utf-8"),
                "agent/pr-5",
            )
            self._fast_forward_origin_to_commit(evidence["commit_sha"], evidence["branch"])
            self._sync_root()
            branch = evidence["branch"]
            discover = json.dumps(
                [{"number": 8, "url": "https://example/repo/pull/8", "state": "MERGED",
                  "headRefName": branch}]
            )
            view = json.dumps(
                {"number": 8, "state": "MERGED", "merged": True,
                 "mergeCommit": {"oid": evidence["commit_sha"]},
                 "baseRefName": "main", "headRefName": branch,
                 "url": "https://example/repo/pull/8"}
            )
            result = await self._resolve(
                canonicalization, project_obj, evidence["commit_sha"], (discover, view)
            )
            self.assertEqual(result["verdict"], "CANONICAL")
            self.assertTrue(result["canonical"]["candidate_is_ancestor"])
            self.assertEqual(result["canonicalization_method"], "exact_commit_ancestor")
            # The landing SHA is the candidate itself (fast-forward merge), so it
            # is an ancestor of the canonical tip: the two ancestry facts agree
            # here but are reported as distinct fields.
            self.assertEqual(result["canonical"]["landing_sha"], evidence["commit_sha"])
            self.assertTrue(result["canonical"]["landing_is_ancestor"])
            self.assertTrue(result["pull_request"]["merged"])
            self.assertEqual(
                result["pull_request"]["merge_commit_sha"], evidence["commit_sha"]
            )

        asyncio.run(exercise())

    # 6. INDETERMINATE: commit object is not present locally (no fetch allowed).
    def test_indeterminate_commit_absent(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("x\n", encoding="utf-8"),
                "agent/absent-6",
            )
            result = await self._resolve(canonicalization, project_obj, "1" * 40)
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertFalse(result["commit_present_locally"])

        asyncio.run(exercise())

    # 7. INDETERMINATE: commit present but no institutional provenance links it.
    def test_indeterminate_no_provenance(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, _e = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("p\n", encoding="utf-8"),
                "agent/noprovenance-7",
            )
            # base_head exists locally (the repo's initial commit) but no job
            # committed it, so provenance is absent even though it is canonical.
            result = await self._resolve(canonicalization, project_obj, self.base_head)
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertTrue(result["commit_present_locally"])
            self.assertFalse(result["provenance"]["job_present"])
            self.assertTrue(result["canonical"]["candidate_is_ancestor"])

        asyncio.run(exercise())

    # 8. INDETERMINATE: commit present + job present, but content does not match
    #    the verified candidate fingerprint (a tampered verification record).
    def test_indeterminate_content_mismatch(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            jobs, _v, _p, _pr, _s, _c, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("z\n", encoding="utf-8"),
                "agent/mismatch-8",
            )
            # Tamper the persisted verification record's diff_fingerprint so the
            # re-derived commit fingerprint no longer matches the stored evidence.
            state_root = self.settings.job_worktree_root / ".state"
            conn = store.open_state_db(state_root / "bridge.db")
            with conn:
                conn.execute(
                    "UPDATE verifications SET diff_fingerprint = ? WHERE verification_id = ?",
                    ("0" * 64, evidence["verification_id"]),
                )
            conn.close()
            # A fresh resolver sees the tampered row (new connection, new snapshot).
            locks = ProjectLockManager()
            pr = PullRequestManager(self.settings, locks, jobs, projects=(project_obj,))
            sync = SyncManager(self.settings, locks, jobs, projects=(project_obj,))
            canonicalization = CanonicalizationResolver(
                self.settings, locks, jobs, pr, sync, projects=(project_obj,)
            )
            result = await self._resolve(
                canonicalization, project_obj, evidence["commit_sha"]
            )
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertTrue(result["provenance"]["job_present"])
            self.assertEqual(result["content_check"]["status"], "mismatched")
            self.assertNotEqual(
                result["content_check"]["rederived_fingerprint"],
                result["content_check"]["verified_diff_fingerprint"],
            )

        asyncio.run(exercise())

    # 9. INDETERMINATE: canonical default-branch tip cannot be resolved from origin.
    def test_indeterminate_default_branch_unresolved(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("d\n", encoding="utf-8"),
                "agent/nohead-9",
            )
            self._delete_origin_head_symref()
            result = await self._resolve(canonicalization, project_obj, evidence["commit_sha"])
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(result["canonical"]["status"], "default_unresolved")
            self.assertIsNone(result["canonical"]["default_branch"])

        asyncio.run(exercise())

    # 10. Security: no absolute private paths leak into the reconciliation result.
    def test_redaction_no_absolute_paths_leaked(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("r\n", encoding="utf-8"),
                "agent/redact-10",
            )
            result = await self._resolve(canonicalization, project_obj, evidence["commit_sha"])
            serialized = json.dumps(result, default=str)
            self.assertNotIn(str(self.base), serialized)
            self.assertNotIn(str(self.root), serialized)
            self.assertNotIn(str(self.settings.job_worktree_root), serialized)
            self.assertNotIn(str(self.settings.projects_config_path), serialized)
            # remote_url is never echoed back, even when an audit trail exists.
            self.assertNotIn("remote_url", serialized)
            for entry in result.get("audit_trail", []):
                self.assertNotIn("remote_url", entry)

        asyncio.run(exercise())

    # 11. Security: the resolver never fetches, pushes, merges, tags, or moves a ref.
    def test_never_fetches_pushes_merges_or_moves_refs(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text("s\n", encoding="utf-8"),
                "agent/readonly-11",
            )
            recorded: list[tuple[str, ...]] = []
            orig_run_git = git_module.run_git
            orig_git_bytes = verification_module._git_bytes

            async def spy_run_git(root: Path, *args: str, timeout_seconds: int = 30) -> str:
                recorded.append(("run_git",) + tuple(args))
                return await orig_run_git(root, *args, timeout_seconds=timeout_seconds)

            async def spy_git_bytes(root: Path, *args: str, timeout_seconds: int = 30) -> bytes:
                recorded.append(("_git_bytes",) + tuple(args))
                return await orig_git_bytes(root, *args, timeout_seconds=timeout_seconds)

            before = self._for_each_ref()
            with mock.patch("local_dev_mcp.introspection.run_git", new=spy_run_git), \
                 mock.patch("local_dev_mcp.verification.run_git", new=spy_run_git), \
                 mock.patch("local_dev_mcp.verification._git_bytes", new=spy_git_bytes):
                result = await self._resolve(
                    canonicalization, project_obj, evidence["commit_sha"]
                )
            after = self._for_each_ref()

            # Every git subcommand issued was read-only.
            subcommands = {call[1] for call in recorded if len(call) > 1}
            self.assertTrue(subcommands, "expected the resolver to issue git reads")
            self.assertTrue(subcommands.issubset(_READ_ONLY_GIT), subcommands)
            forbidden = {
                "fetch", "push", "pull", "merge", "rebase", "tag", "update-ref",
                "reset", "checkout", "commit", "rm", "clean", "stash",
            }
            self.assertEqual(subcommands & forbidden, set(), subcommands)
            # No ref in the canonical checkout's .git was created, moved, or deleted.
            self.assertEqual(before, after)
            # And the reconciliation still produced a coherent verdict.
            self.assertIn(result["verdict"], {"CANONICAL", "NOT_CANONICAL", "INDETERMINATE"})

        asyncio.run(exercise())

    # 12. NOT_CANONICAL: PR closed without merge (gh mocked CLOSED, merged=false).
    def test_not_canonical_with_closed_unmerged_pr(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "closed\n", encoding="utf-8"
                ),
                "agent/pr-closed",
            )
            branch = evidence["branch"]
            discover = json.dumps(
                [
                    {
                        "number": 9,
                        "url": "https://example/repo/pull/9",
                        "state": "CLOSED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 9,
                    "state": "CLOSED",
                    "merged": False,
                    "mergeCommit": None,
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/9",
                }
            )
            result = await self._resolve(
                canonicalization,
                project_obj,
                evidence["commit_sha"],
                (discover, view),
            )
            self.assertEqual(result["verdict"], "NOT_CANONICAL")
            self.assertEqual(result["canonicalization_method"], "not_merged")
            self.assertTrue(result["pull_request"]["present"])
            self.assertEqual(result["pull_request"]["number"], 9)
            self.assertFalse(result["pull_request"]["merged"])
            self.assertIsNone(result["pull_request"]["merge_commit_sha"])
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertIsNone(result["canonical"]["landing_sha"])
            self.assertIsNone(result["canonical"]["landing_is_ancestor"])

        asyncio.run(exercise())

    # 13. NOT_CANONICAL: PR merged but its landing SHA is no longer an ancestor
    #     of the current canonical tip (reverted / history rewritten past it).
    def test_not_canonical_merged_pr_landing_no_longer_ancestor(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "rewind\n", encoding="utf-8"
                ),
                "agent/pr-rewind",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # The PR merges by fast-forwarding origin/main to the candidate.
            self._fast_forward_origin_to_commit(commit, branch)
            self._sync_root()
            # Then canonical history is rewritten back past the landing (a revert
            # or force-update). The candidate object remains local from the sync.
            clone = self.base / "rewind-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            self._config(clone)
            subprocess.run(
                ["git", "-C", str(clone), "reset", "--hard", self.base_head],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "--force", "origin", "main"],
                check=True,
            )
            discover = json.dumps(
                [
                    {
                        "number": 10,
                        "url": "https://example/repo/pull/10",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 10,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": {"oid": commit},
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/10",
                }
            )
            result = await self._resolve(
                canonicalization, project_obj, commit, (discover, view)
            )
            self.assertEqual(result["verdict"], "NOT_CANONICAL")
            self.assertEqual(
                result["canonicalization_method"], "landing_not_in_canonical_history"
            )
            self.assertTrue(result["pull_request"]["merged"])
            self.assertEqual(result["pull_request"]["merge_commit_sha"], commit)
            self.assertEqual(result["canonical"]["landing_sha"], commit)
            self.assertFalse(result["canonical"]["landing_is_ancestor"])
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertEqual(result["canonical"]["current_head"], self.base_head)

        asyncio.run(exercise())

    # 14. CANONICAL: publication branch deleted after a successful merge.
    #     Branch deletion must not erase canonicalization (ancestry-based) nor
    #     the institutional publication record.
    def test_canonical_after_branch_deleted_post_merge(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "delbranch\n", encoding="utf-8"
                ),
                "agent/pr-delbranch",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # The merge lands by fast-forwarding origin/main to the candidate.
            self._fast_forward_origin_to_commit(commit, branch)
            self._sync_root()
            # The remote feature branch is then deleted (common post-merge cleanup).
            clone = self.base / "del-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "origin", "--delete", branch],
                check=True,
            )
            result = await self._resolve(canonicalization, project_obj, commit)
            self.assertEqual(result["verdict"], "CANONICAL")
            self.assertEqual(
                result["canonicalization_method"], "exact_commit_ancestor"
            )
            self.assertTrue(result["canonical"]["candidate_is_ancestor"])
            self.assertEqual(result["canonical"]["current_head"], commit)
            # The branch is gone from origin, but the verdict is still CANONICAL
            # (driven by ancestry, not by branch presence) and the publication
            # record in bridge.db is preserved.
            self.assertFalse(result["remote_branch"]["present"])
            self.assertTrue(result["provenance"]["publication_present"])
            self.assertEqual(result["provenance"]["publication_branch"], branch)

        asyncio.run(exercise())

    # 15. CANONICAL via rebase content-equivalence: the PR landed as a DIFFERENT
    #     commit on a moved base, so the candidate SHA is not an ancestor and no
    #     tree-identical commit exists on canonical history (whole-tree equality
    #     fails), but the candidate's changed-path blob matches the landing
    #     commit's -> Case B (merged_pr_content_equivalent).
    def test_canonical_via_rebase_content_equivalence(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "rebase\n", encoding="utf-8"
                ),
                "agent/pr-rebase",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # Move origin/main forward with an UNRELATED commit (new base).
            clone = self.base / "rbase-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            self._config(clone)
            (clone / "unrelated.txt").write_text("u\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "unrelated.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "unrelated base move"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"],
                check=True,
            )
            # Rebase landing: apply the candidate's exact change on the moved base.
            (clone / "candidate.txt").write_text("rebase\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "candidate.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "Rebase landing"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"],
                check=True,
            )
            landing = rev(self.bare, "main")
            # Populate the canonical checkout's object store (fixture only).
            self._sync_root()
            discover = json.dumps(
                [
                    {
                        "number": 11,
                        "url": "https://example/repo/pull/11",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 11,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": {"oid": landing},
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/11",
                }
            )
            result = await self._resolve(
                canonicalization, project_obj, commit, (discover, view)
            )
            self.assertEqual(result["verdict"], "CANONICAL")
            self.assertEqual(
                result["canonicalization_method"], "merged_pr_content_equivalent"
            )
            # The candidate SHA is NOT an ancestor (rebase landed a new commit)
            # and no tree-identical commit exists (the base moved)...
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertFalse(result["canonical"]["content_equivalent"])
            self.assertIsNone(result["canonical"]["matched_canonical_commit"])
            # ...but the landing SHA is an ancestor and the changed-path content
            # matches at landing, which is what makes it canonical (Case B).
            self.assertEqual(result["canonical"]["landing_sha"], landing)
            self.assertTrue(result["canonical"]["landing_is_ancestor"])
            self.assertEqual(result["content_equivalence"]["status"], "equivalent")
            self.assertEqual(result["content_equivalence"]["mismatches"], [])
            self.assertEqual(result["content_equivalence"]["landing_commit"], landing)

        asyncio.run(exercise())


    # 16. NOT_CANONICAL: PR merged and the landing SHA IS an ancestor of the
    #     canonical tip, but the candidate's changed-path content does NOT match
    #     the landing commit's state -> landing_content_mismatch (the substantive
    #     content did not land as-is). Required mission scenario.
    def test_not_canonical_landing_content_mismatch(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "orig\n", encoding="utf-8"
                ),
                "agent/pr-mismatch-16",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # The PR "lands" as a DIFFERENT commit on main whose candidate.txt
            # content differs from the candidate's -> the landing is an ancestor
            # of the tip but the changed-path content does not match.
            clone = self.base / "mismatch-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            self._config(clone)
            (clone / "candidate.txt").write_text("DIFFERENT\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "candidate.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "landing differs"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"],
                check=True,
            )
            landing = rev(self.bare, "main")
            self._sync_root()
            discover = json.dumps(
                [
                    {
                        "number": 16,
                        "url": "https://example/repo/pull/16",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 16,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": {"oid": landing},
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/16",
                }
            )
            result = await self._resolve(
                canonicalization, project_obj, commit, (discover, view)
            )
            self.assertEqual(result["verdict"], "NOT_CANONICAL")
            self.assertEqual(
                result["canonicalization_method"], "landing_content_mismatch"
            )
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertIsNone(result["canonical"]["matched_canonical_commit"])
            self.assertEqual(result["canonical"]["landing_sha"], landing)
            self.assertTrue(result["canonical"]["landing_is_ancestor"])
            self.assertEqual(result["content_equivalence"]["status"], "mismatch")
            self.assertIn("candidate.txt", result["content_equivalence"]["mismatches"])
            self.assertEqual(
                result["content_equivalence"]["landing_commit"], landing
            )

        asyncio.run(exercise())

    # 17. INDETERMINATE: PR merged but its landing SHA is not present in the local
    #     object store (and a fetch is forbidden). Required mission scenario.
    def test_indeterminate_merged_pr_landing_sha_not_local(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "lnlocal\n", encoding="utf-8"
                ),
                "agent/pr-landing-absent-17",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # gh reports a merged PR whose landing SHA is a 40-hex object that was
            # never fetched into the canonical checkout (read-only invariant).
            phantom = "deadbeef" * 5
            discover = json.dumps(
                [
                    {
                        "number": 17,
                        "url": "https://example/repo/pull/17",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 17,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": {"oid": phantom},
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/17",
                }
            )
            result = await self._resolve(
                canonicalization, project_obj, commit, (discover, view)
            )
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(
                result["canonicalization_method"], "insufficient_evidence"
            )
            self.assertTrue(result["pull_request"]["merged"])
            self.assertEqual(result["pull_request"]["merge_commit_sha"], phantom)
            self.assertEqual(result["canonical"]["landing_sha"], phantom)
            self.assertIsNone(result["canonical"]["landing_is_ancestor"])
            self.assertFalse(result["canonical"]["landing_present_locally"])
            self.assertTrue(
                any("landing sha" in w.lower() for w in result["warnings"])
            )

        asyncio.run(exercise())

    # 18. INDETERMINATE: the canonical tip is known remotely (ls-remote succeeds)
    #     but its object is not present in the local store (origin advanced past
    #     the canonical checkout's last sync, and a fetch is forbidden). Ancestry
    #     cannot be verified, so the verdict must not be a false NOT_CANONICAL.
    def test_indeterminate_remote_tip_not_local(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "tipnotlocal\n", encoding="utf-8"
                ),
                "agent/tip-absent-18",
            )
            # Advance origin/main with an unrelated commit created in a separate
            # clone and NEVER sync it into the canonical checkout.
            clone = self.base / "tip-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            self._config(clone)
            (clone / "advance.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "advance.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "advance main"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True
            )
            new_tip = rev(self.bare, "main")
            result = await self._resolve(
                canonicalization, project_obj, evidence["commit_sha"]
            )
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(
                result["canonicalization_method"], "insufficient_evidence"
            )
            self.assertEqual(result["canonical"]["status"], "remote_tip_not_local")
            self.assertEqual(result["canonical"]["default_branch"], "main")
            self.assertEqual(result["canonical"]["current_head"], new_tip)
            self.assertFalse(result["canonical"]["candidate_is_ancestor"])
            self.assertTrue(
                any("not present locally" in w.lower() for w in result["warnings"])
            )

        asyncio.run(exercise())

    # 19. INDETERMINATE: the PR layer (gh) was unavailable (rate limit / network /
    #     auth). The PR merge/landing state is genuinely unknown, so "not merged"
    #     must not be asserted with certainty.
    def test_indeterminate_gh_unavailable(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "ghdown\n", encoding="utf-8"
                ),
                "agent/gh-down-19",
            )
            gh_mock = mock.AsyncMock(side_effect=PullRequestError("gh down"))
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                result = await canonicalization.resolve(
                    project_obj, evidence["commit_sha"]
                )
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(
                result["canonicalization_method"], "insufficient_evidence"
            )
            self.assertIsNotNone(result["pull_request"])
            self.assertEqual(result["pull_request"]["error"], "gh pr list failed")
            self.assertIsNone(result["pull_request"]["merged"])
            self.assertTrue(
                any("gh" in w.lower() and "unavailable" in w.lower()
                    for w in result["warnings"])
            )

        asyncio.run(exercise())

    # 20. INDETERMINATE: gh reports merged=True but no mergeCommit (null / not a
    #     dict), so the landing SHA cannot be determined. The PR is merged but the
    #     landing commit cannot be located -> INDETERMINATE, not "not merged".
    def test_indeterminate_merged_pr_unknown_landing_sha(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "nolanding\n", encoding="utf-8"
                ),
                "agent/pr-no-landing-20",
            )
            branch = evidence["branch"]
            discover = json.dumps(
                [
                    {
                        "number": 20,
                        "url": "https://example/repo/pull/20",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 20,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": None,
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/20",
                }
            )
            result = await self._resolve(
                canonicalization,
                project_obj,
                evidence["commit_sha"],
                (discover, view),
            )
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(
                result["canonicalization_method"], "insufficient_evidence"
            )
            self.assertTrue(result["pull_request"]["merged"])
            self.assertIsNone(result["pull_request"]["merge_commit_sha"])
            self.assertIsNone(result["canonical"]["landing_sha"])
            self.assertTrue(
                any("landing sha" in w.lower() for w in result["warnings"])
            )

        asyncio.run(exercise())

    # 21. INDETERMINATE: the landing commit is on canonical history (landing SHA
    #     is an ancestor) but the per-path content check could not be performed
    #     (a transient ls-tree failure on an already-present commit). The check is
    #     inconclusive, so the verdict must not be a false NOT_CANONICAL.
    def test_indeterminate_content_equivalence_unavailable(self) -> None:
        async def exercise() -> None:
            project_obj = policy(self.root)
            *_, canonicalization, evidence = await self._pipeline(
                project_obj,
                lambda root: (root / "candidate.txt").write_text(
                    "ceunavail\n", encoding="utf-8"
                ),
                "agent/pr-ce-unavail-21",
            )
            commit = evidence["commit_sha"]
            branch = evidence["branch"]
            # Rebase-style landing: move main with an unrelated commit, then apply
            # the candidate's change as a NEW landing commit (candidate not an
            # ancestor, no tree-identical commit, but the landing SHA is).
            clone = self.base / "ceu-clone"
            subprocess.run(
                ["git", "clone", "-q", str(self.bare), str(clone)], check=True
            )
            self._config(clone)
            (clone / "unrelated.txt").write_text("u\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "unrelated.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "move base"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True
            )
            (clone / "candidate.txt").write_text("ceunavail\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(clone), "add", "candidate.txt"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "commit", "-qm", "rebase landing"], check=True
            )
            subprocess.run(
                ["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True
            )
            landing = rev(self.bare, "main")
            self._sync_root()
            discover = json.dumps(
                [
                    {
                        "number": 21,
                        "url": "https://example/repo/pull/21",
                        "state": "MERGED",
                        "headRefName": branch,
                    }
                ]
            )
            view = json.dumps(
                {
                    "number": 21,
                    "state": "MERGED",
                    "merged": True,
                    "mergeCommit": {"oid": landing},
                    "baseRefName": "main",
                    "headRefName": branch,
                    "url": "https://example/repo/pull/21",
                }
            )
            # Inject a transient ls-tree failure on the resolver's read path only,
            # so _content_equivalence cannot read the (present) landing tree.
            orig_run_git = introspection_module.run_git

            async def failing_ls_tree(root: Path, *args: str, timeout_seconds: int = 30) -> str:
                if len(args) >= 1 and args[0] == "ls-tree":
                    raise git_module.GitCommandError("simulated ls-tree failure")
                return await orig_run_git(
                    root, *args, timeout_seconds=timeout_seconds
                )

            gh_mock = mock.AsyncMock(side_effect=[discover, view])
            with mock.patch(
                "local_dev_mcp.introspection.run_git", new=failing_ls_tree
            ), mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                result = await canonicalization.resolve(project_obj, commit)
            self.assertEqual(result["verdict"], "INDETERMINATE")
            self.assertEqual(
                result["canonicalization_method"], "insufficient_evidence"
            )
            self.assertEqual(result["content_equivalence"]["status"], "unavailable")
            self.assertTrue(result["canonical"]["landing_is_ancestor"])
            self.assertEqual(result["canonical"]["landing_sha"], landing)
            self.assertTrue(
                any("content equivalence" in w.lower()
                    for w in result["warnings"])
            )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()