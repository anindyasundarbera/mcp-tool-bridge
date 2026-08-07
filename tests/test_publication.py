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
from local_dev_mcp.verification import (
    PublicationError,
    PublicationManager,
    VerificationCommitManager,
)

from tests.test_verification import EditingExecutor, wait_completed


BRANCH = "agent/cks-002b-formal-specification"


def init_repo(root: Path) -> str:
    """Create a repo on ``main`` and return the initial HEAD (the verified base)."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (root / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Initial"], check=True)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def add_origin(root: Path, bare: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", str(bare)], check=True
    )
    subprocess.run(["git", "-C", str(root), "push", "-q", "origin", "main"], check=True)


def policy(root: Path, **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Publication tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "claude_edit",
            "verification_commit",
            "push_development_commit",
        ],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": ["{python}", "-c", "pass"]},
        ],
        "protected_paths": [".git", ".env"],
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


def remote_ref(bare: Path, name: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
    )
    sha = result.stdout.strip()
    return sha or None


class PublicationTests(unittest.TestCase):
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

    async def _prepare(
        self,
        policy_obj: ProjectPolicy,
        edit: Any,
    ) -> tuple[DevelopmentJobManager, VerificationCommitManager, PublicationManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings,
            locks,
            EditingExecutor(edit),  # type: ignore[arg-type]
            projects=(policy_obj,),
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy_obj,)
        )
        publication = PublicationManager(
            self.settings, locks, jobs, projects=(policy_obj,)
        )
        started = await jobs.start(policy_obj, "make candidate", "high")
        completed = await wait_completed(jobs, policy_obj, str(started["job_id"]))
        self.assertEqual(completed["status"], "completed")
        return jobs, verification, publication, completed

    async def _verify_and_commit(
        self,
        verification: VerificationCommitManager,
        policy_obj: ProjectPolicy,
        completed: dict[str, Any],
    ) -> dict[str, Any]:
        verified = await verification.verify(
            policy_obj, str(completed["job_id"]), str(completed["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        committed = await verification.commit(
            policy_obj,
            str(completed["job_id"]),
            str(verified["verification_id"]),
            str(verified["base_head"]),
            str(verified["diff_fingerprint"]),
            list(verified["paths"]),
            "MCPB publication candidate",
        )
        self.assertEqual(committed["status"], "committed")
        return {
            "job_id": str(completed["job_id"]),
            "commit_sha": committed["commit_sha"],
            "base_head": str(verified["base_head"]),
            "fingerprint": str(verified["diff_fingerprint"]),
        }

    def test_publishes_exact_commit_to_new_branch_without_moving_main(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "docs").mkdir()
                (root / "docs" / "spec.md").write_text("# Spec\n", encoding="utf-8")

            jobs, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)

            result = await publication.publish(
                policy_obj,
                evidence["job_id"],
                evidence["commit_sha"],
                BRANCH,
                evidence["base_head"],
                evidence["fingerprint"],
            )
            self.assertEqual(result["status"], "published")
            self.assertTrue(result["published"])
            self.assertFalse(result["idempotent"])
            self.assertEqual(result["branch_name"], BRANCH)
            self.assertEqual(result["remote_ref"], f"refs/heads/{BRANCH}")
            self.assertEqual(result["commit_sha"], evidence["commit_sha"])
            self.assertEqual(result["remote_url"], str(self.bare))
            # The remote branch points at exactly the detached commit ...
            self.assertEqual(remote_ref(self.bare, BRANCH), evidence["commit_sha"])
            # ... and main on the remote is untouched.
            self.assertEqual(remote_ref(self.bare, "main"), evidence["base_head"])
            # main in the canonical checkout is untouched too.
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.root), "rev-parse", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                self.base_head,
            )
            # The published commit's parent is the verified base.
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.bare), "rev-parse", f"{evidence['commit_sha']}^"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                evidence["base_head"],
            )

        asyncio.run(exercise())

    def test_retrying_same_branch_is_idempotent_and_second_branch_rejected(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            jobs, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            first = await publication.publish(
                policy_obj,
                evidence["job_id"],
                evidence["commit_sha"],
                BRANCH,
                evidence["base_head"],
                evidence["fingerprint"],
            )
            self.assertFalse(first["idempotent"])

            retry = await publication.publish(
                policy_obj,
                evidence["job_id"],
                evidence["commit_sha"],
                BRANCH,
                evidence["base_head"],
                evidence["fingerprint"],
            )
            self.assertTrue(retry["idempotent"])
            self.assertEqual(retry["commit_sha"], evidence["commit_sha"])

            with self.assertRaisesRegex(PublicationError, "different branch"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    "agent/cks-002c-other",
                    evidence["base_head"],
                    evidence["fingerprint"],
                )
            # The second branch was never created.
            self.assertIsNone(remote_ref(self.bare, "agent/cks-002c-other"))

        asyncio.run(exercise())

    def test_rejects_trunk_and_default_branch_names(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            # main/master are rejected outright.
            for trunk in ("main", "master", "MAIN"):
                with self.assertRaisesRegex(PublicationError, "trunk branch"):
                    await publication.publish(
                        policy_obj,
                        evidence["job_id"],
                        evidence["commit_sha"],
                        trunk,
                        evidence["base_head"],
                        evidence["fingerprint"],
                    )
            # The repo default branch (here configured to "develop") is rejected
            # even though it is not main/master.
            subprocess.run(
                ["git", "-C", str(self.root), "push", "-q", "origin", "main:refs/heads/develop"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(self.root), "fetch", "-q", "origin"], check=True
            )
            subprocess.run(
                ["git", "-C", str(self.root), "remote", "set-head", "origin", "develop"],
                check=True,
            )
            with self.assertRaisesRegex(PublicationError, "default branch"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    "develop",
                    evidence["base_head"],
                    evidence["fingerprint"],
                )

        asyncio.run(exercise())

    def test_rejects_existing_remote_branch_and_wrong_evidence(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            # A pre-existing remote branch (at some other commit) blocks publication.
            subprocess.run(
                ["git", "-C", str(self.bare), "update-ref", "refs/heads/feature", evidence["base_head"]],
                check=True,
            )
            with self.assertRaisesRegex(PublicationError, "remote branch already exists"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    "feature",
                    evidence["base_head"],
                    evidence["fingerprint"],
                )
            # commit_sha must match the bridge-created detached commit.
            with self.assertRaisesRegex(PublicationError, "does not match"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["base_head"],
                    "agent/cks-002d-sha",
                    evidence["base_head"],
                    evidence["fingerprint"],
                )
            # A wrong fingerprint has no matching used verification record.
            with self.assertRaisesRegex(PublicationError, "no matching used verification"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    "agent/cks-002e-fp",
                    evidence["base_head"],
                    "0" * 64,
                )
            # A wrong base HEAD is rejected.
            with self.assertRaisesRegex(PublicationError, "base HEAD"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    "agent/cks-002f-bh",
                    "0" * 40,
                    evidence["fingerprint"],
                )

        asyncio.run(exercise())

    def test_rejects_bad_branch_names_and_missing_origin(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            bad_names = [
                "refs/heads/x",
                "HEAD",
                "HEAD/x",
                ".hidden",
                "x..y",
                "x.lock",
                "x/y.lock",
                "@",
                "x@{y",
                "x y",
                "x" + "/" + "y" * 250,
                "x\t y",
                "a//b",
                "/leading",
                "trailing/",
            ]
            for name in bad_names:
                with self.assertRaisesRegex(PublicationError, "branch_name"):
                    await publication.publish(
                        policy_obj,
                        evidence["job_id"],
                        evidence["commit_sha"],
                        name,
                        evidence["base_head"],
                        evidence["fingerprint"],
                    )

        asyncio.run(exercise())

    def test_origin_missing_is_rejected(self) -> None:
        async def exercise() -> None:
            # A second repo with no origin configured.
            other_root = self.base / "norepo"
            init_repo(other_root)
            policy_obj = policy(other_root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            with self.assertRaisesRegex(PublicationError, "origin remote is not configured"):
                await publication.publish(
                    policy_obj,
                    evidence["job_id"],
                    evidence["commit_sha"],
                    BRANCH,
                    evidence["base_head"],
                    evidence["fingerprint"],
                )

        asyncio.run(exercise())

    def test_recovering_after_remote_branch_vanished_repushes(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, completed = await self._prepare(
                policy_obj, edit
            )
            evidence = await self._verify_and_commit(verification, policy_obj, completed)
            first = await publication.publish(
                policy_obj,
                evidence["job_id"],
                evidence["commit_sha"],
                BRANCH,
                evidence["base_head"],
                evidence["fingerprint"],
            )
            self.assertFalse(first["idempotent"])
            # Simulate the remote branch being deleted after the prior publish.
            subprocess.run(
                ["git", "-C", str(self.bare), "update-ref", "-d", f"refs/heads/{BRANCH}"],
                check=True,
            )
            self.assertIsNone(remote_ref(self.bare, BRANCH))
            recovered = await publication.publish(
                policy_obj,
                evidence["job_id"],
                evidence["commit_sha"],
                BRANCH,
                evidence["base_head"],
                evidence["fingerprint"],
            )
            self.assertFalse(recovered["idempotent"])
            self.assertEqual(remote_ref(self.bare, BRANCH), evidence["commit_sha"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()