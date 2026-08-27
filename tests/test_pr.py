"""M-CC-01/02/05: Draft pull-request creation, state confirmation, idempotency.

Tests exercise the PullRequestManager with a mocked ``gh`` CLI (no real GitHub,
no network): discovery finds an existing PR and updates it; no existing PR
creates a draft; remote branch / PR state is confirmed; merge is never issued;
and concurrent calls produce one PR. Uses a local bare origin for the published
branch.
"""

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.pr import PullRequestError, PullRequestManager
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.verification import (
    PublicationManager,
    VerificationCommitManager,
)

from tests.test_verification import EditingExecutor, wait_completed
from tests.test_publication import add_origin, init_repo, policy


class PullRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.base_head = init_repo(self.root)
        self.bare = self.base / "remote.git"
        add_origin(self.root, self.bare)
        # Point refs/remotes/origin/HEAD at main so the PR base resolves.
        subprocess.run(
            ["git", "-C", str(self.root), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
            check=True,
        )
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
    ) -> tuple[DevelopmentJobManager, VerificationCommitManager, PublicationManager, PullRequestManager, dict[str, Any]]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings, locks, EditingExecutor(edit), projects=(policy_obj,)
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(policy_obj,)
        )
        publication = PublicationManager(
            self.settings, locks, jobs, projects=(policy_obj,)
        )
        pr = PullRequestManager(
            self.settings, locks, jobs, projects=(policy_obj,)
        )
        started = await jobs.start(policy_obj, "make candidate", "high")
        completed = await wait_completed(jobs, policy_obj, str(started["job_id"]))
        self.assertEqual(completed["status"], "completed")
        return jobs, verification, publication, pr, completed

    async def _publish(
        self,
        verification: VerificationCommitManager,
        publication: PublicationManager,
        policy_obj: ProjectPolicy,
        completed: dict[str, Any],
        branch: str,
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
            "MCPB PR candidate",
        )
        self.assertEqual(committed["status"], "committed")
        published = await publication.publish(
            policy_obj,
            str(completed["job_id"]),
            committed["commit_sha"],
            branch,
            str(verified["base_head"]),
            str(verified["diff_fingerprint"]),
        )
        self.assertEqual(published["status"], "published")
        return {
            "job_id": str(completed["job_id"]),
            "commit_sha": committed["commit_sha"],
            "base_head": str(verified["base_head"]),
            "fingerprint": str(verified["diff_fingerprint"]),
        }

    def test_no_existing_pr_creates_draft(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-1"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )

            gh_mock = mock.AsyncMock(side_effect=[
                # discover -> no existing PR (empty output)
                "",
                # create -> returns the PR url
                "https://github.com/example/repo/pull/1",
            ])
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                result = await pr.open_pull_request(
                    policy_obj,
                    info["job_id"],
                    info["base_head"],
                    info["fingerprint"],
                    "My draft PR",
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["pr_state"]["action"], "created")
            self.assertEqual(result["pr_state"]["state"], "open")
            self.assertTrue(result["draft"])
            self.assertTrue(result["remote_branch_state"]["present"])
            # The create call must supply an explicit ``--body`` (BridgeLab's
            # detached-publication model has no local feature branch, so
            # ``--fill`` cannot infer a commit range and must be absent) while
            # keeping the explicit ``--title``, ``--head``, ``--base``, and
            # ``--draft``. It must never merge.
            calls = [c.args for c in gh_mock.call_args_list]
            create_call = next(c for c in calls if "create" in c)
            self.assertIn("--body", create_call)
            self.assertNotIn("--fill", create_call)
            self.assertIn("--title", create_call)
            self.assertIn("My draft PR", create_call)
            self.assertIn("--head", create_call)
            self.assertIn(branch, create_call)
            self.assertIn("--base", create_call)
            self.assertIn("--draft", create_call)
            # Body is the BridgeLab-owned deterministic description (no Git
            # history inference, no worktree paths).
            body = create_call[create_call.index("--body") + 1]
            self.assertIn("Created by BridgeLab", body)
            self.assertIn("source branch: agent/pr-test-1", body)
            self.assertFalse(any("merge" in c for c in calls))

        asyncio.run(exercise())

    def test_concurrent_open_pull_request_creates_once_edits_once(self) -> None:
        """M-CC-05 tail: racing open_pull_request calls yield one create + one edit.

        Two ``open_pull_request`` calls run concurrently via ``asyncio.gather``
        under one shared ``PullRequestManager`` with a mocked ``_gh``. The
        per-project lock serializes the discover-then-create/update critical
        section, so the callers cannot double-create: the first discovers no PR
        and issues exactly one ``gh pr create``; the second (after the first
        releases the lock) discovers the now-existing PR and issues exactly one
        ``gh pr edit``. Never a merge.
        """
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-concurrent"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )

            gh_mock = mock.AsyncMock(side_effect=[
                # First caller: discover -> no existing PR, then create -> url.
                "",
                "https://github.com/example/repo/pull/42",
                # Second caller (after the first releases the project lock):
                # discover -> the now-existing PR, then edit -> success.
                '[{"number": 42, "url": "https://github.com/example/repo/pull/42", "state": "open", "headRefName": "agent/pr-test-concurrent"}]',
                "",
            ])
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                results = await asyncio.gather(
                    pr.open_pull_request(
                        policy_obj,
                        info["job_id"],
                        info["base_head"],
                        info["fingerprint"],
                        "Concurrent PR",
                    ),
                    pr.open_pull_request(
                        policy_obj,
                        info["job_id"],
                        info["base_head"],
                        info["fingerprint"],
                        "Concurrent PR",
                    ),
                )

            self.assertEqual([r["status"] for r in results], ["ok", "ok"])
            self.assertEqual(
                sorted(r["pr_state"]["action"] for r in results),
                ["created", "updated"],
            )
            self.assertTrue(all(r["remote_branch_state"]["present"] for r in results))
            calls = [c.args for c in gh_mock.call_args_list]
            # Exactly one gh pr create, one gh pr edit, two gh pr list discovers,
            # and nothing that merges.
            self.assertEqual(len(calls), 4)
            self.assertEqual(sum("create" in c for c in calls), 1)
            self.assertEqual(sum("edit" in c for c in calls), 1)
            self.assertEqual(
                sum(len(c) > 2 and c[1] == "pr" and c[2] == "list" for c in calls),
                2,
            )
            self.assertFalse(any("merge" in c for c in calls))

        asyncio.run(exercise())

    def test_non_draft_create_has_body_no_draft_flag(self) -> None:
        """Non-draft PR creation supplies --body/--title, omits --draft/--fill."""
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-nondraft"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )

            gh_mock = mock.AsyncMock(side_effect=[
                # discover -> no existing PR (empty output)
                "",
                # create -> returns the PR url
                "https://github.com/example/repo/pull/2",
            ])
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                result = await pr.open_pull_request(
                    policy_obj,
                    info["job_id"],
                    info["base_head"],
                    info["fingerprint"],
                    "Ready for review",
                    draft=False,
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["pr_state"]["action"], "created")
            self.assertFalse(result["draft"])
            calls = [c.args for c in gh_mock.call_args_list]
            create_call = next(c for c in calls if "create" in c)
            self.assertIn("--body", create_call)
            self.assertNotIn("--fill", create_call)
            self.assertIn("--title", create_call)
            self.assertIn("Ready for review", create_call)
            self.assertIn("--head", create_call)
            self.assertIn(branch, create_call)
            self.assertIn("--base", create_call)
            self.assertNotIn("--draft", create_call)
            self.assertFalse(any("merge" in c for c in calls))

        asyncio.run(exercise())

    def test_create_does_not_require_local_feature_branch(self) -> None:
        """PR creation works with no local feature branch (detached publication).

        BridgeLab publishes a detached commit to the remote branch while the
        canonical checkout stays on ``main``; no local branch named
        ``<branch>`` exists. The explicit ``--body`` keeps ``gh pr create`` from
        inspecting a local commit range (``--fill`` would fail here with an
        ambiguous-argument error). This test deletes any local branch ref and
        confirms creation still succeeds.
        """
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-nolocalbranch"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )
            # Ensure no local branch named <branch> exists in the canonical
            # checkout (mirrors BridgeLab's detached-publication model).
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "--list", branch],
                check=True,
                capture_output=True,
            )
            # The canonical checkout must remain on main, not the feature branch.
            head_ref = subprocess.run(
                ["git", "-C", str(self.root), "symbolic-ref", "--short", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(head_ref, "main")
            self.assertNotIn(branch, subprocess.run(
                ["git", "-C", str(self.root), "branch", "--list"],
                check=True, capture_output=True, text=True,
            ).stdout)

            gh_mock = mock.AsyncMock(side_effect=[
                # discover -> no existing PR (empty output)
                "",
                # create -> returns the PR url
                "https://github.com/example/repo/pull/3",
            ])
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                result = await pr.open_pull_request(
                    policy_obj,
                    info["job_id"],
                    info["base_head"],
                    info["fingerprint"],
                    "Detached candidate PR",
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["pr_state"]["action"], "created")
            # The PR was created despite the feature branch not existing locally.
            create_call = next(
                c for c in [c.args for c in gh_mock.call_args_list]
                if "create" in c
            )
            self.assertIn("--body", create_call)
            self.assertNotIn("--fill", create_call)

        asyncio.run(exercise())

    def test_existing_pr_is_updated(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-2"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )

            with mock.patch(
                "local_dev_mcp.pr._gh",
                new=mock.AsyncMock(side_effect=[
                    # discover -> existing PR
                    '[{"number": 7, "url": "https://github.com/example/repo/pull/7", "state": "open", "headRefName": "agent/pr-test-2"}]',
                    # edit -> success
                    "",
                ]),
            ):
                result = await pr.open_pull_request(
                    policy_obj,
                    info["job_id"],
                    info["base_head"],
                    info["fingerprint"],
                    "Updated title",
                )
            self.assertEqual(result["pr_state"]["action"], "updated")
            self.assertEqual(result["pr_state"]["number"], 7)
            self.assertEqual(result["pr_state"]["state"], "open")

        asyncio.run(exercise())

    def test_vanished_remote_branch_is_rejected(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-3"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )
            # Delete the remote branch so it is "vanished".
            subprocess.run(
                ["git", "-C", str(self.bare), "update-ref", "-d", f"refs/heads/{branch}"],
                check=True,
            )
            with self.assertRaisesRegex(PullRequestError, "vanished"):
                await pr.open_pull_request(
                    policy_obj,
                    info["job_id"],
                    info["base_head"],
                    info["fingerprint"],
                    "My draft PR",
                )

        asyncio.run(exercise())

    def test_requires_publication_record(self) -> None:
        async def exercise() -> None:
            policy_obj = policy(self.root)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, _, pr, completed = await self._prepare(policy_obj, edit)
            # Commit the job but do NOT publish it, so there is no publication
            # record for the PR tool to find.
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
                "MCPB PR candidate",
            )
            self.assertEqual(committed["status"], "committed")
            with self.assertRaisesRegex(PullRequestError, "no publication record"):
                await pr.open_pull_request(
                    policy_obj,
                    str(completed["job_id"]),
                    str(completed["base_head"]),
                    str(verified["diff_fingerprint"]),
                    "My draft PR",
                )

        asyncio.run(exercise())

    def test_git_failure_is_wrapped_as_pull_request_error(self) -> None:
        """A git failure confirming remote state surfaces as PullRequestError."""
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-gitfail"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )
            # Force _remote_branch_sha to raise a VerificationError.
            from local_dev_mcp.verification import VerificationError

            with mock.patch(
                "local_dev_mcp.pr._remote_branch_sha",
                new=mock.AsyncMock(side_effect=VerificationError("git exploded")),
            ):
                with self.assertRaisesRegex(PullRequestError, "remote branch state"):
                    await pr.open_pull_request(
                        policy_obj,
                        info["job_id"],
                        info["base_head"],
                        info["fingerprint"],
                        "My draft PR",
                    )

        asyncio.run(exercise())

    def test_gh_failure_during_discovery_propagates(self) -> None:
        """A gh failure during discovery is not mistaken for 'no PR'."""
        async def exercise() -> None:
            policy_obj = policy(self.root)
            branch = "agent/pr-test-ghfail"

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, verification, publication, pr, completed = await self._prepare(
                policy_obj, edit
            )
            info = await self._publish(
                verification, publication, policy_obj, completed, branch
            )
            # The first _gh call (discovery) raises; it must propagate, not be
            # treated as "no PR" (which would cause a duplicate create).
            gh_mock = mock.AsyncMock(side_effect=PullRequestError("gh auth failed"))
            with mock.patch("local_dev_mcp.pr._gh", new=gh_mock):
                with self.assertRaisesRegex(PullRequestError, "gh auth failed"):
                    await pr.open_pull_request(
                        policy_obj,
                        info["job_id"],
                        info["base_head"],
                        info["fingerprint"],
                        "My draft PR",
                    )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
