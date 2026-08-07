import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectCapability, ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager, ProjectRegistry, ProjectRegistryError
from local_dev_mcp.review import ReviewError, ReviewManager
from local_dev_mcp.verification import VerificationCommitManager

from tests.support import initialize_git_repository
from tests.test_verification import EditingExecutor, wait_completed


def review_project(root: Path, command: list[str], **overrides: object) -> ProjectPolicy:
    payload: dict[str, object] = {
        "id": "test-project",
        "display_name": "Test Project",
        "description": "Review tests",
        "root": str(root),
        "capabilities": [
            "repository_read",
            "claude_edit",
            "verification_commit",
            "candidate_review",
        ],
        "commit_roots": ["."],
        "verification_commands": [
            {"id": "core", "argv": command, "python_path": ["src"]}
        ],
        "protected_paths": [".git", ".env"],
        "requires_review": True,
        "allow_bridge_reviewer": True,
    }
    payload.update(overrides)
    return ProjectPolicy.model_validate(payload)


class ReviewExecutor:
    """Fake bridge-spawned reviewer for run_independent_review tests.

    Captures the call so tests can assert separation of duties (no resume, a
    fresh root), and can simulate drift, failure, or a non-JSON verdict.
    """

    def __init__(
        self,
        verdict: dict[str, Any] | None = None,
        status: str = "completed",
        drift: bool = False,
        result_text: str | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verdict = verdict or {
            "summary": "looks good",
            "issues": [],
            "recommendation": "approve",
        }
        self.status = status
        self.drift = drift
        self.result_text = result_text

    async def execute_review(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: str,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]:
        assert resume_session_id is None, "reviewer must never resume"
        assert root is not None, "reviewer must run in the candidate worktree"
        self.calls.append(
            {
                "root": root,
                "specification": specification,
                "effort": effort,
                "resume_session_id": resume_session_id,
            }
        )
        if self.drift:
            (root / "candidate.txt").write_text(
                "mutated during review\n", encoding="utf-8"
            )
        text = (
            self.result_text
            if self.result_text is not None
            else json.dumps(self.verdict)
        )
        return {
            "status": self.status,
            "project_id": project.id,
            "result": text,
            "exit_code": 0,
            "is_error": False,
        }


class ReviewManagerTests(unittest.TestCase):
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
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                "VERIFICATION_COMMAND_TIMEOUT_SECONDS": "2",
                "MAX_VERIFICATION_OUTPUT_BYTES": "64",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _managers(
        self,
        project: ProjectPolicy,
        edit: Any,
        review_executor: Any = None,
    ) -> tuple[ProjectLockManager, DevelopmentJobManager, VerificationCommitManager, ReviewManager]:
        locks = ProjectLockManager()
        jobs = DevelopmentJobManager(
            self.settings, locks, EditingExecutor(edit), projects=(project,)
        )
        verification = VerificationCommitManager(
            self.settings, locks, jobs, projects=(project,)
        )
        review = ReviewManager(
            self.settings,
            locks,
            jobs,
            review_executor or ReviewExecutor(),
            projects=(project,),
        )
        return locks, jobs, verification, review

    async def _completed_job(
        self, jobs: DevelopmentJobManager, project: ProjectPolicy, edit: Any
    ) -> dict[str, Any]:
        started = await jobs.start(project, "make candidate", "high")
        completed = await wait_completed(jobs, project, str(started["job_id"]))
        assert completed["status"] == "completed"
        return completed

    # -- list / read ---------------------------------------------------------

    def test_list_candidate_changes_shows_paths_and_redacts_worktree(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            result = await review.list_candidate_changes(
                project, str(completed["job_id"])
            )
            self.assertEqual(result["base_head"], completed["base_head"])
            self.assertEqual(len(result["paths"]), 1)
            entry = result["paths"][0]
            self.assertEqual(entry["path"], "candidate.txt")
            self.assertFalse(entry["deleted"])
            self.assertEqual(entry["size_bytes"], len("candidate\n"))
            self.assertIn("sha256", entry)
            # The worktree absolute path must never appear in any output.
            self.assertNotIn(str(self.settings.job_worktree_root), json.dumps(result))
            self.assertNotIn(str(self.base), json.dumps(result))

        asyncio.run(exercise())

    def test_list_candidate_changes_rejects_non_completed_job(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            started = await jobs.start(project, "running", "high")
            with self.assertRaisesRegex(ReviewError, "only a completed"):
                await review.list_candidate_changes(project, str(started["job_id"]))

        asyncio.run(exercise())

    def test_read_candidate_file_is_scoped_to_changed_paths(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            # A changed path is readable.
            read = await review.read_candidate_file(project, job_id, "candidate.txt")
            self.assertEqual(read["content"], "candidate\n")
            self.assertIn("sha256", read)
            # An unchanged-but-existing path is NOT part of the candidate set.
            with self.assertRaisesRegex(ReviewError, "not part of the candidate changes"):
                await review.read_candidate_file(project, job_id, "README.md")
            # Traversal and absolute paths are rejected by normalization.
            with self.assertRaisesRegex(ReviewError, "normalized relative path"):
                await review.read_candidate_file(project, job_id, "../etc/passwd")
            with self.assertRaisesRegex(ReviewError, "normalized relative path"):
                await review.read_candidate_file(project, job_id, "/etc/passwd")
            self.assertNotIn(str(self.settings.job_worktree_root), json.dumps(read))

        asyncio.run(exercise())

    def test_read_candidate_file_rejects_outside_commit_roots(self) -> None:
        async def exercise() -> None:
            project = review_project(
                self.root,
                ["{python}", "-c", "pass"],
                commit_roots=["src"],
            )

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            with self.assertRaisesRegex(ReviewError, "outside approved commit roots"):
                await review.read_candidate_file(
                    project, str(completed["job_id"]), "candidate.txt"
                )

        asyncio.run(exercise())

    def test_open_failure_does_not_leak_absolute_worktree_path(self) -> None:
        # Regression guard: if the anchored root open (os.open(worktree,
        # O_DIRECTORY)) fails, the OSError's .filename is the absolute worktree
        # path. _open_candidate_read must convert that to a path-free
        # VerificationError so the absolute path never reaches tool output.
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            absolute = str(self.settings.job_worktree_root / project.id / job_id)

            real_open = os.open

            def fail_only_root_open(path, flags, *args, **kwargs):
                # Fail only the anchored root open of THIS worktree (an O_DIRECTORY
                # open with no dir_fd whose path is exactly the worktree root).
                # The leaf open passes dir_fd; _ensure_private_directory opens the
                # filesystem anchor and other dirs, not this worktree; the git
                # subprocess pipes never use O_DIRECTORY — all those delegate.
                if (
                    kwargs.get("dir_fd") is None
                    and (flags & os.O_DIRECTORY)
                    and os.fspath(path) == absolute
                ):
                    raise OSError(13, "Permission denied", str(path))
                return real_open(path, flags, *args, **kwargs)

            with mock.patch(
                "local_dev_mcp.verification.os.open", side_effect=fail_only_root_open
            ):
                with self.assertRaises(ReviewError) as ctx:
                    await review.list_candidate_changes(project, job_id)
            # The absolute worktree path must not appear in the tool error.
            self.assertNotIn(absolute, str(ctx.exception))
            self.assertIn("candidate worktree could not be opened", str(ctx.exception))

        asyncio.run(exercise())

    def test_read_candidate_file_rejects_symlink_candidate(self) -> None:
        # Regression guard for the end-to-end-anchored open (_open_candidate_read):
        # a changed path that is a symlink, or that sits beneath a symlinked
        # parent, must be rejected — the anchored walk refuses both, so neither
        # a leaf nor a parent symlink can redirect the read outside the worktree.
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "target.txt").write_text("real\n", encoding="utf-8")
                os.symlink("target.txt", root / "link.txt")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            with self.assertRaisesRegex(ReviewError, "symbolic links"):
                await review.read_candidate_file(project, job_id, "link.txt")
            with self.assertRaisesRegex(ReviewError, "symbolic links"):
                await review.list_candidate_changes(project, job_id)

        asyncio.run(exercise())

    def test_read_candidate_file_line_range_and_sha256(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            full = await review.read_candidate_file(project, job_id, "candidate.txt")
            ranged = await review.read_candidate_file(
                project, job_id, "candidate.txt", start_line=2, end_line=2
            )
            self.assertEqual(ranged["content"], "line2\n")
            self.assertEqual(ranged["start_line"], 2)
            self.assertEqual(ranged["end_line"], 2)
            self.assertEqual(ranged["total_lines"], 3)
            with self.assertRaisesRegex(ReviewError, "expected_sha256 does not match"):
                await review.read_candidate_file(
                    project,
                    job_id,
                    "candidate.txt",
                    expected_sha256="0" * 64,
                )
            with self.assertRaisesRegex(ReviewError, "supplied together"):
                await review.read_candidate_file(
                    project, job_id, "candidate.txt", start_line=1
                )

        asyncio.run(exercise())

    def test_read_candidate_file_reports_deletion(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "src" / "marker.py").unlink()

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            read = await review.read_candidate_file(
                project, str(completed["job_id"]), "src/marker.py"
            )
            self.assertTrue(read["deleted"])
            self.assertNotIn("content", read)

        asyncio.run(exercise())

    # -- record ---------------------------------------------------------------

    def test_record_candidate_review_binds_state_and_is_retrievable(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(
                project, job_id, completed["base_head"]
            )
            recorded = await review.record_candidate_review(
                project,
                job_id,
                {"summary": "ok", "issues": [], "recommendation": "approve"},
                reviewer_id="human-reviewer",
            )
            self.assertEqual(recorded["status"], "recorded")
            self.assertRegex(str(recorded["review_id"]), r"^[0-9a-f]{32}$")
            self.assertEqual(recorded["base_head"], verified["base_head"])
            self.assertEqual(recorded["diff_fingerprint"], verified["diff_fingerprint"])
            self.assertEqual(recorded["paths"], verified["paths"])
            self.assertEqual(recorded["reviewer_id"], "human-reviewer")
            fetched = await review.get_candidate_review(
                project, job_id, recorded["review_id"]
            )
            self.assertEqual(fetched["verdict"]["summary"], "ok")
            self.assertFalse(fetched["used"])

        asyncio.run(exercise())

    def test_record_candidate_review_rejects_empty_non_dict_and_oversized(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            with self.assertRaisesRegex(ReviewError, "must be a structured object"):
                await review.record_candidate_review(project, job_id, "not a dict")
            with self.assertRaisesRegex(ReviewError, "must not be empty"):
                await review.record_candidate_review(project, job_id, {})
            with self.assertRaisesRegex(ReviewError, "must not be empty"):
                await review.record_candidate_review(project, job_id, {"x": ""})
            with self.assertRaisesRegex(ReviewError, "character limit"):
                await review.record_candidate_review(
                    project, job_id, {"summary": "x" * 30000}
                )

        asyncio.run(exercise())

    def test_get_candidate_review_redacts_paths_embedded_in_verdict(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            await verification.verify(project, job_id, completed["base_head"])

            # A caller (or reviewer) can embed the absolute worktree path and the
            # project root in its verdict. get_candidate_review must redact them.
            worktree_path = self.settings.job_worktree_root / project.id / job_id
            leaking_verdict = {
                "summary": f"reviewed files under {worktree_path}",
                "issues": [f"root at {project.root} leaked"],
                "recommendation": "approve",
            }
            recorded = await review.record_candidate_review(
                project, job_id, leaking_verdict, reviewer_id="x"
            )
            fetched = await review.get_candidate_review(
                project, job_id, recorded["review_id"]
            )
            blob = json.dumps(fetched)
            # The raw secret paths must never appear in the tool output.
            self.assertNotIn(str(worktree_path), blob)
            self.assertNotIn(str(self.settings.job_worktree_root), blob)
            self.assertNotIn(str(project.root), blob)
            # The verdict is still readable, with the paths replaced.
            self.assertEqual(
                fetched["verdict"]["summary"],
                f"reviewed files under <redacted-path>",
            )
            self.assertIn("<redacted-path>", fetched["verdict"]["issues"][0])

        asyncio.run(exercise())

    def test_mark_used_failure_reverts_flag_and_keeps_review_reusable(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(
                project, job_id, completed["base_head"]
            )
            recorded = await review.record_candidate_review(
                project, job_id, {"summary": "ok"}, reviewer_id="x"
            )

            # Force the durable write inside mark_used to fail.
            original = review._persist_review

            def failing_persist(record: Any) -> None:
                raise RuntimeError("disk full")

            review._persist_review = failing_persist  # type: ignore[assignment]
            try:
                with self.assertRaisesRegex(RuntimeError, "disk full"):
                    await review.mark_used(recorded["review_id"])
            finally:
                review._persist_review = original  # type: ignore[assignment]

            # The in-memory flag reverted, so memory and disk agree (both
            # unused), and the review is still valid evidence for a commit.
            record = review._reviews[recorded["review_id"]]
            self.assertFalse(record.used)
            ok = await review.validate_for_commit(
                project,
                job_id,
                recorded["review_id"],
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
            )
            self.assertFalse(ok.used)

        asyncio.run(exercise())

    def test_record_candidate_review_rejects_reserved_bridge_reviewer_id(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            # The "bridge-reviewer" marker is reserved for run_independent_review,
            # so a caller cannot forge a record that looks attested-independent.
            with self.assertRaisesRegex(ReviewError, "reserved"):
                await review.record_candidate_review(
                    project, job_id, {"summary": "ok"}, reviewer_id="bridge-reviewer"
                )
            # A leading/trailing-whitespace forgery is also rejected after strip.
            with self.assertRaisesRegex(ReviewError, "reserved"):
                await review.record_candidate_review(
                    project, job_id, {"summary": "ok"}, reviewer_id=" bridge-reviewer "
                )

        asyncio.run(exercise())

    def test_record_candidate_review_survives_restart(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings, locks, EditingExecutor(edit), projects=(project,)
            )
            review = ReviewManager(
                self.settings, locks, jobs, ReviewExecutor(), projects=(project,)
            )
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            recorded = await review.record_candidate_review(
                project, job_id, {"summary": "ok"}
            )
            review_id = recorded["review_id"]

            # A fresh manager re-hydrates the review record from the same db.
            locks_b = ProjectLockManager()
            jobs_b = DevelopmentJobManager(
                self.settings, locks_b, EditingExecutor(edit), projects=(project,)
            )
            review_b = ReviewManager(
                self.settings, locks_b, jobs_b, ReviewExecutor(), projects=(project,)
            )
            fetched = await review_b.get_candidate_review(project, job_id, review_id)
            self.assertEqual(fetched["review_id"], review_id)
            self.assertEqual(fetched["verdict"]["summary"], "ok")

        asyncio.run(exercise())

    # -- run_independent_review ----------------------------------------------

    def test_run_independent_review_requires_allow_bridge_reviewer(self) -> None:
        async def exercise() -> None:
            project = review_project(
                self.root, ["{python}", "-c", "pass"], allow_bridge_reviewer=False
            )

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("x", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit, ReviewExecutor())
            completed = await self._completed_job(jobs, project, edit)
            with self.assertRaisesRegex(ReviewError, "does not allow a bridge-spawned"):
                await review.run_independent_review(
                    project, str(completed["job_id"]), "high"
                )

        asyncio.run(exercise())

    def test_run_independent_review_uses_fresh_read_only_session(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])
            executor = ReviewExecutor()

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit, executor)
            completed = await self._completed_job(jobs, project, edit)
            result = await review.run_independent_review(
                project, str(completed["job_id"]), "high"
            )
            self.assertEqual(result["status"], "recorded")
            self.assertRegex(str(result["review_id"]), r"^[0-9a-f]{32}$")
            # Separation of duties: the reviewer was a fresh session (no resume)
            # launched in the candidate worktree.
            self.assertEqual(len(executor.calls), 1)
            self.assertIsNone(executor.calls[0]["resume_session_id"])
            self.assertIsInstance(executor.calls[0]["root"], Path)
            self.assertNotEqual(executor.calls[0]["root"], self.root)
            # The verdict is persisted and retrievable.
            fetched = await review.get_candidate_review(
                project, str(completed["job_id"]), result["review_id"]
            )
            self.assertEqual(fetched["verdict"]["summary"], "looks good")
            self.assertEqual(fetched["reviewer_id"], "bridge-reviewer")
            self.assertNotIn(
                str(self.settings.job_worktree_root), json.dumps(result)
            )

        asyncio.run(exercise())

    def test_run_independent_review_rejects_drift(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])
            executor = ReviewExecutor(drift=True)

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit, executor)
            completed = await self._completed_job(jobs, project, edit)
            with self.assertRaisesRegex(ReviewError, "candidate changed during"):
                await review.run_independent_review(
                    project, str(completed["job_id"]), "high"
                )

        asyncio.run(exercise())

    def test_run_independent_review_rejects_failed_reviewer(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])
            executor = ReviewExecutor(status="failed")

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit, executor)
            completed = await self._completed_job(jobs, project, edit)
            with self.assertRaisesRegex(ReviewError, "did not complete"):
                await review.run_independent_review(
                    project, str(completed["job_id"]), "high"
                )

        asyncio.run(exercise())

    def test_run_independent_review_accepts_non_json_verdict(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])
            executor = ReviewExecutor(result_text="free-form review text")

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, _, review = self._managers(project, edit, executor)
            completed = await self._completed_job(jobs, project, edit)
            result = await review.run_independent_review(
                project, str(completed["job_id"]), "high"
            )
            fetched = await review.get_candidate_review(
                project, str(completed["job_id"]), result["review_id"]
            )
            # A non-JSON verdict is wrapped, never an empty record.
            self.assertEqual(fetched["verdict"]["summary"], "free-form review text")

        asyncio.run(exercise())

    # -- commit gate ---------------------------------------------------------

    def test_commit_gate_requires_review_id(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(project, job_id, completed["base_head"])
            # The commit gate (server-side) would raise before calling commit;
            # validate_for_commit itself rejects a missing id via the server.
            # Here we assert the gate rejects a mismatched review directly.
            with self.assertRaisesRegex(ReviewError, "unknown review_id"):
                await review.validate_for_commit(
                    project,
                    job_id,
                    "0" * 32,
                    verified["base_head"],
                    verified["diff_fingerprint"],
                    list(verified["paths"]),
                )

        asyncio.run(exercise())

    def test_commit_gate_consumes_matching_review_once(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(project, job_id, completed["base_head"])
            recorded = await review.record_candidate_review(
                project, job_id, {"summary": "approve"}
            )
            review_id = recorded["review_id"]

            # validate_for_commit accepts the matching review.
            await review.validate_for_commit(
                project,
                job_id,
                review_id,
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
            )
            # A real commit succeeds and consumes the review.
            committed = await verification.commit(
                project,
                job_id,
                verified["verification_id"],
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
                "MCPB-007 reviewed commit",
            )
            self.assertEqual(committed["status"], "committed")
            await review.mark_used(review_id)
            fetched = await review.get_candidate_review(project, job_id, review_id)
            self.assertTrue(fetched["used"])
            # Single-use: a second validation against the consumed review fails.
            with self.assertRaisesRegex(ReviewError, "already been used"):
                await review.validate_for_commit(
                    project,
                    job_id,
                    review_id,
                    verified["base_head"],
                    verified["diff_fingerprint"],
                    list(verified["paths"]),
                )

        asyncio.run(exercise())

    def test_commit_gate_rejects_mismatched_evidence(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            _, jobs, verification, review = self._managers(project, edit)
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(project, job_id, completed["base_head"])
            recorded = await review.record_candidate_review(
                project, job_id, {"summary": "approve"}
            )
            with self.assertRaisesRegex(ReviewError, "does not match review evidence"):
                await review.validate_for_commit(
                    project,
                    job_id,
                    recorded["review_id"],
                    verified["base_head"],
                    "0" * 64,
                    list(verified["paths"]),
                )
            with self.assertRaisesRegex(ReviewError, "paths must exactly match"):
                await review.validate_for_commit(
                    project,
                    job_id,
                    recorded["review_id"],
                    verified["base_head"],
                    verified["diff_fingerprint"],
                    ["candidate.txt", "extra.txt"],
                )

        asyncio.run(exercise())

    def test_commit_gate_survives_restart_single_use(self) -> None:
        async def exercise() -> None:
            project = review_project(self.root, ["{python}", "-c", "pass"])

            def edit(root: Path) -> None:
                (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")

            locks = ProjectLockManager()
            jobs = DevelopmentJobManager(
                self.settings, locks, EditingExecutor(edit), projects=(project,)
            )
            verification = VerificationCommitManager(
                self.settings, locks, jobs, projects=(project,)
            )
            review = ReviewManager(
                self.settings, locks, jobs, ReviewExecutor(), projects=(project,)
            )
            completed = await self._completed_job(jobs, project, edit)
            job_id = str(completed["job_id"])
            verified = await verification.verify(project, job_id, completed["base_head"])
            recorded = await review.record_candidate_review(
                project, job_id, {"summary": "approve"}
            )
            review_id = recorded["review_id"]

            # Restart: fresh managers re-hydrate job, verification, and review.
            locks_b = ProjectLockManager()
            jobs_b = DevelopmentJobManager(
                self.settings, locks_b, EditingExecutor(edit), projects=(project,)
            )
            verification_b = VerificationCommitManager(
                self.settings, locks_b, jobs_b, projects=(project,)
            )
            review_b = ReviewManager(
                self.settings, locks_b, jobs_b, ReviewExecutor(), projects=(project,)
            )
            await review_b.validate_for_commit(
                project,
                job_id,
                review_id,
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
            )
            committed = await verification_b.commit(
                project,
                job_id,
                verified["verification_id"],
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
                "post-restart reviewed commit",
            )
            self.assertEqual(committed["status"], "committed")
            await review_b.mark_used(review_id)
            with self.assertRaisesRegex(ReviewError, "already been used"):
                await review_b.validate_for_commit(
                    project,
                    job_id,
                    review_id,
                    verified["base_head"],
                    verified["diff_fingerprint"],
                    list(verified["paths"]),
                )

        asyncio.run(exercise())

    # -- capability gate -----------------------------------------------------

    def test_candidate_review_capability_gate(self) -> None:
        project_with = review_project(self.root, ["{python}", "-c", "pass"])
        project_without = ProjectPolicy.model_validate(
            {
                "id": "other",
                "display_name": "Other",
                "description": "No review",
                "root": str(self.root),
                "capabilities": ["repository_read", "claude_edit", "verification_commit"],
                "commit_roots": ["."],
                "verification_commands": [
                    {"id": "core", "argv": ["{python}", "-c", "pass"]}
                ],
            }
        )
        registry = ProjectRegistry(
            {project_with.id: project_with, project_without.id: project_without}
        )
        # A project with candidate_review resolves.
        resolved = registry.resolve("test-project", ProjectCapability.CANDIDATE_REVIEW)
        self.assertEqual(resolved.id, "test-project")
        # A project without candidate_review is denied.
        with self.assertRaisesRegex(ProjectRegistryError, "does not allow candidate_review"):
            registry.resolve("other", ProjectCapability.CANDIDATE_REVIEW)


if __name__ == "__main__":
    unittest.main()