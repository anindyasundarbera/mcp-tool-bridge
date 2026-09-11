"""Tests for working_tree_candidate_capture (MCPB-012).

Every test uses a real temporary Git repository and exercises the full
two-step preflight/capture contract, the read-only overlay, drift detection,
protected-exclusion enforcement, and downstream-gate compatibility.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from local_dev_mcp.config import Settings
from local_dev_mcp.jobs import DevelopmentJobManager
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.review import ReviewError, ReviewManager
from local_dev_mcp.verification import (
    VerificationCommitManager,
    VerificationError,
    _digest_candidate_paths,
    candidate_changed_paths,
)
from local_dev_mcp.working_tree_capture import (
    WorkingTreeCaptureError,
    WorkingTreeCaptureManager,
    _overlaps,
    _pattern_matches,
)

from tests.support import initialize_git_repository
from tests.test_jobs import (
    ImmediateExecutor,
    settings as base_settings,
    wait_for_status,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _status(root: Path) -> list[str]:
    """Sorted porcelain paths (no status codes) for snapshot comparisons."""
    out = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--no-renames"],
        check=True,
        capture_output=True,
    ).stdout
    return sorted(p.decode("utf-8")[3:] for p in out.split(b"\0") if p)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _make_project(
    root: Path,
    *,
    include: tuple[str, ...] = ("AGENTS.md", "docs/**", "script.sh"),
    protected: tuple[str, ...] = (".reasonix/a.json", ".reasonix/b.json"),
    capabilities: tuple[str, ...] = (
        "repository_read",
        "claude_edit",
        "working_tree_candidate_capture",
    ),
) -> ProjectPolicy:
    return ProjectPolicy.model_validate(
        {
            "id": "test-project",
            "display_name": "Test",
            "description": "Capture tests",
            "root": str(root),
            "capabilities": list(capabilities),
            "commit_roots": ["."],
            "protected_paths": [".git", ".env"],
            "verification_commands": [
                {"id": "git-diff-check", "argv": ["git", "diff", "--check", "HEAD", "--"]}
            ],
            "working_tree_capture_scope": {
                "include_paths": list(include),
                "protected_exclude_paths": list(protected),
            },
        }
    )


class CaptureTestCase(unittest.TestCase):
    """Base fixture: a temp git repo with a rich baseline + managers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "repo"
        self._build_baseline()
        self.settings = base_settings(self.base)
        self.project = _make_project(self.root)
        self.locks = ProjectLockManager()
        self.jobs = DevelopmentJobManager(
            self.settings, self.locks, ImmediateExecutor(), projects=(self.project,)
        )
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- repo helpers ------------------------------------------------------ #

    def _build_baseline(self) -> None:
        initialize_git_repository(self.root)  # commits README.md
        (self.root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("a-1\n", encoding="utf-8")
        (self.root / "docs" / "b.md").write_text("b-1\n", encoding="utf-8")
        (self.root / "script.sh").write_text("echo hi\n", encoding="utf-8")
        (self.root / "notes.md").write_text("notes-1\n", encoding="utf-8")
        (self.root / ".reasonix").mkdir()
        (self.root / ".reasonix" / "a.json").write_text("{}\n", encoding="utf-8")
        (self.root / ".reasonix" / "b.json").write_text("{}\n", encoding="utf-8")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-qm", "baseline")

    def _write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _rm(self, rel: str) -> None:
        (self.root / rel).unlink()

    def _chmod_x(self, rel: str) -> None:
        path = self.root / rel
        os.chmod(path, path.stat().st_mode | 0o111)

    def _stage(self, rel: str) -> None:
        _git(self.root, "add", "--", rel)

    # -- the standard dirty state used by the happy-path + scope tests ----- #

    def _make_dirty(self) -> None:
        self._write("AGENTS.md", "# Agents edited\n")          # M unstaged
        self._write("docs/a.md", "a-2\n")                       # M unstaged
        self._write("docs/new.md", "new\n")                     # ?? untracked
        self._rm("docs/b.md")                                   # D unstaged
        self._chmod_x("script.sh")                               # mode change
        self._write(".reasonix/a.json", "{\"x\":1}\n")            # protected M
        self._write(".reasonix/b.json", "{\"y\":2}\n")            # protected M
        self._write("notes.md", "notes-2\n")                    # unrelated M

    # -- capture convenience ---------------------------------------------- #

    def _capture(self, **kwargs: Any) -> dict[str, Any]:
        scope = self.project.working_tree_capture_scope
        return asyncio.run(
            self.capture.capture(
                self.project,
                list(scope.include_paths),
                list(scope.protected_exclude_paths),
                **kwargs,
            )
        )

    def _preflight(self, **kwargs: Any) -> dict[str, Any]:
        scope = self.project.working_tree_capture_scope
        return asyncio.run(
            self.capture.preflight(
                self.project,
                list(scope.include_paths),
                list(scope.protected_exclude_paths),
                **kwargs,
            )
        )

    def _audit_rows(self) -> list[dict[str, Any]]:
        return self.capture.audit(self.project.id)


# --------------------------------------------------------------------------- #
# Pattern / overlap unit tests
# --------------------------------------------------------------------------- #


class PatternTests(unittest.TestCase):
    def test_exact_match(self) -> None:
        self.assertTrue(_pattern_matches("AGENTS.md", "AGENTS.md"))
        self.assertFalse(_pattern_matches("AGENTS.md", "docs/AGENTS.md"))

    def test_recursive_glob(self) -> None:
        self.assertTrue(_pattern_matches("docs/**", "docs/a.md"))
        self.assertTrue(_pattern_matches("docs/**", "docs/sub/b.md"))
        self.assertFalse(_pattern_matches("docs/**", "other.md"))

    def test_overlap_detection(self) -> None:
        self.assertTrue(_overlaps("docs/**", "docs/x.md"))
        self.assertTrue(_overlaps("docs/**", "docs/sub/**"))
        self.assertFalse(_overlaps("docs/**", "AGENTS.md"))
        self.assertFalse(_overlaps("a.md", "b.md"))


# --------------------------------------------------------------------------- #
# The 17 capture cases
# --------------------------------------------------------------------------- #


class CaptureHappyPathTests(CaptureTestCase):
    def test_01_captures_unstaged_modified_tracked_file(self) -> None:
        self._write("AGENTS.md", "# edited\n")
        # narrow scope to just AGENTS.md for an unambiguous single-path capture
        self.project = _make_project(self.root, include=("AGENTS.md",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        result = self._capture()
        self.assertEqual(result["status"], "captured")
        self.assertEqual([m["path"] for m in result["manifest"]], ["AGENTS.md"])
        self.assertEqual(result["manifest"][0]["kind"], "modified")
        self.assertIn("candidate_fingerprint", result)
        self.assertTrue(result["job_id"])

    def test_02_captures_staged_change(self) -> None:
        # A brand-new file staged into the index (porcelain "A ") — a staged
        # addition distinct from an untracked addition (test_03).
        self._write("docs/staged.md", "staged\n")
        self._stage("docs/staged.md")
        self.project = _make_project(self.root, include=("docs/**",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        result = self._capture()
        self.assertEqual(result["manifest"][0]["path"], "docs/staged.md")
        self.assertEqual(result["manifest"][0]["kind"], "added")
        # The descriptor distinguishes staged (index) from unstaged.
        self.assertTrue(result["manifest"][0]["staged"])
        self.assertFalse(result["manifest"][0]["unstaged"])

    def test_03_captures_untracked_file(self) -> None:
        self._write("docs/new.md", "new\n")
        self.project = _make_project(self.root, include=("docs/**",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        result = self._capture()
        self.assertEqual(result["manifest"][0]["path"], "docs/new.md")
        self.assertEqual(result["manifest"][0]["kind"], "added")

    def test_04_captures_deletion(self) -> None:
        self._rm("docs/b.md")
        self.project = _make_project(self.root, include=("docs/**",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        result = self._capture()
        self.assertEqual(result["manifest"][0]["path"], "docs/b.md")
        self.assertEqual(result["manifest"][0]["kind"], "deleted")
        self.assertIsNone(result["manifest"][0]["content_sha256"])

    def test_05_captures_executable_bit_change(self) -> None:
        self._chmod_x("script.sh")
        self.project = _make_project(self.root, include=("script.sh",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        result = self._capture()
        self.assertEqual(result["manifest"][0]["path"], "script.sh")
        self.assertEqual(result["manifest"][0]["mode"], "100755")

    def test_06_rejects_changed_symlink(self) -> None:
        # Replace a tracked regular file with a symlink — a typechange the
        # capture must refuse (verification rejects symlinks).
        target = self.root / "README.md"
        (self.root / "AGENTS.md").unlink()
        os.symlink(target.name, self.root / "AGENTS.md")
        self.project = _make_project(self.root, include=("AGENTS.md",), protected=())
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        with self.assertRaises(WorkingTreeCaptureError):
            self._capture()
        # No candidate retained on rejection.
        self.assertEqual(self.jobs._jobs, {})
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "rejected")


class ScopeAndExclusionTests(CaptureTestCase):
    def test_07_protected_excluded_paths_never_captured(self) -> None:
        self._make_dirty()
        result = self._capture()
        manifest_paths = {m["path"] for m in result["manifest"]}
        protected = {m["path"] for m in result["protected_exclude_manifest"]}
        # Protected .reasonix files appear in the protected manifest...
        self.assertEqual(
            protected, {".reasonix/a.json", ".reasonix/b.json"}
        )
        # ...never in the candidate manifest...
        self.assertNotIn(".reasonix/a.json", manifest_paths)
        self.assertNotIn(".reasonix/b.json", manifest_paths)
        # ...and unrelated dirty files (notes.md) are never silently included.
        self.assertNotIn("notes.md", manifest_paths)
        self.assertNotIn("notes.md", protected)
        # The expected selected set is exactly the in-scope dirty paths.
        self.assertEqual(
            manifest_paths,
            {"AGENTS.md", "docs/a.md", "docs/new.md", "docs/b.md", "script.sh"},
        )

    def test_08_rejects_widened_include_paths(self) -> None:
        self._make_dirty()
        scope = self.project.working_tree_capture_scope
        with self.assertRaises(WorkingTreeCaptureError):
            asyncio.run(
                self.capture.capture(
                    self.project,
                    list(scope.include_paths) + ["notes.md"],  # widened
                    list(scope.protected_exclude_paths),
                )
            )

    def test_09_rejects_narrowed_include_paths(self) -> None:
        self._make_dirty()
        scope = self.project.working_tree_capture_scope
        with self.assertRaises(WorkingTreeCaptureError):
            asyncio.run(
                self.capture.capture(
                    self.project,
                    ["AGENTS.md"],  # narrowed: drops docs/** + script.sh
                    list(scope.protected_exclude_paths),
                )
            )

    def test_10_rejects_invalid_caller_paths(self) -> None:
        # Invalid caller paths are rejected at scope normalization, before the
        # canonical checkout is even read — so a clean repo is sufficient.
        scope = self.project.working_tree_capture_scope
        for bad in (
            ["/etc/passwd"],            # absolute
            ["../escape"],              # traversal
            ["AGENTS.md", "AGENTS.md"],  # duplicate
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(WorkingTreeCaptureError):
                    asyncio.run(
                        self.capture.capture(
                            self.project,
                            bad,
                            list(scope.protected_exclude_paths),
                        )
                    )

    def test_11_rejects_overlapping_include_patterns_in_policy(self) -> None:
        # docs/** and docs/a.md overlap — policy validation must reject the
        # scope at capture time (the overlap check runs in _resolve_scope).
        self._write("docs/a.md", "x\n")
        self.project = _make_project(
            self.root, include=("docs/**", "docs/a.md"), protected=()
        )
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        with self.assertRaises(WorkingTreeCaptureError):
            self._capture()

    def test_12_rejects_include_path_selecting_no_changes(self) -> None:
        # docs/unclean.md is an include pattern but nothing under it changed
        # and it is not a changed path itself.
        self._write("AGENTS.md", "edited\n")
        self.project = _make_project(
            self.root, include=("AGENTS.md", "docs/missing/**"), protected=()
        )
        self.capture = WorkingTreeCaptureManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        with self.assertRaises(WorkingTreeCaptureError) as cm:
            self._capture()
        self.assertIn("selects no changed paths", str(cm.exception))


class DriftTests(CaptureTestCase):
    """Drift is simulated by patching the overlay step to mutate the source
    checkout between the pre-fingerprint and the post-revalidation."""

    def _capture_with_drift(self, mutator: Any) -> None:
        original = __import__(
            "local_dev_mcp.working_tree_capture", fromlist=["_overlay_path"]
        )._overlay_path
        state = {"fired": False}

        def drifting(worktree_path: Path, descriptor: Any) -> None:
            original(worktree_path, descriptor)
            if not state["fired"]:
                state["fired"] = True
                mutator()

        with patch(
            "local_dev_mcp.working_tree_capture._overlay_path", side_effect=drifting
        ):
            with self.assertRaises(WorkingTreeCaptureError):
                self._capture()

    def test_13_drift_head_moves_during_capture(self) -> None:
        self._make_dirty()

        def move_head() -> None:
            _git(self.root, "commit", "--allow-empty", "-qm", "drift")

        self._capture_with_drift(move_head)
        # No candidate retained, no worktree left behind.
        self.assertEqual(self.jobs._jobs, {})
        rows = self._audit_rows()
        self.assertEqual(rows[0]["status"], "rejected")
        # No leftover worktree directories.
        self.assertEqual(list((self.settings.job_worktree_root / self.project.id).glob("*")), [])

    def test_14_drift_selected_file_changes_during_capture(self) -> None:
        self._make_dirty()

        def touch_selected() -> None:
            self._write("AGENTS.md", "# mid-capture mutation\n")

        self._capture_with_drift(touch_selected)
        self.assertEqual(self.jobs._jobs, {})
        self.assertEqual(self._audit_rows()[0]["status"], "rejected")

    def test_15_drift_protected_file_changes_during_capture(self) -> None:
        self._make_dirty()

        def touch_protected() -> None:
            self._write(".reasonix/a.json", "{\"drift\":true}\n")

        self._capture_with_drift(touch_protected)
        self.assertEqual(self.jobs._jobs, {})
        self.assertEqual(self._audit_rows()[0]["status"], "rejected")


class ContractAndGateTests(CaptureTestCase):
    def test_16_two_step_preflight_then_capture_contract(self) -> None:
        self._make_dirty()
        pre = self._preflight()
        self.assertEqual(pre["status"], "preflight")
        self.assertIsNone(pre["capture_id"])
        base_head = pre["base_head"]
        fingerprint = pre["working_tree_fingerprint"]
        # Echoing the exact preflight state succeeds.
        ok = asyncio.run(
            self.capture.capture(
                self.project,
                list(self.project.working_tree_capture_scope.include_paths),
                list(self.project.working_tree_capture_scope.protected_exclude_paths),
                expected_base_head=base_head,
                expected_working_tree_fingerprint=fingerprint,
            )
        )
        self.assertEqual(ok["status"], "captured")
        self.assertEqual(ok["base_head"], base_head)
        self.assertEqual(ok["working_tree_fingerprint"], fingerprint)
        # A wrong expected fingerprint fails and retains no candidate.
        with self.assertRaises(WorkingTreeCaptureError):
            asyncio.run(
                self.capture.capture(
                    self.project,
                    list(self.project.working_tree_capture_scope.include_paths),
                    list(self.project.working_tree_capture_scope.protected_exclude_paths),
                    expected_base_head=base_head,
                    expected_working_tree_fingerprint="0" * 64,
                )
            )

    def test_17_captured_candidate_is_gate_compatible_and_source_unmodified(self) -> None:
        self._make_dirty()
        before_status = _status(self.root)
        before_head = _head(self.root)
        before_protected_a = (self.root / ".reasonix/a.json").read_bytes()
        result = self._capture()
        after_status = _status(self.root)
        after_head = _head(self.root)
        # --- The canonical checkout was never modified. ---
        self.assertEqual(before_status, after_status)
        self.assertEqual(before_head, after_head)
        self.assertEqual((self.root / ".reasonix/a.json").read_bytes(), before_protected_a)
        # --- A retained candidate exists and is complete. ---
        job = asyncio.run(self.jobs.get(self.project, result["job_id"]))
        self.assertEqual(str(job["status"]), "completed")
        # The candidate's worktree changed-path set equals the manifest.
        worktree = self.jobs._jobs[result["job_id"]].worktree
        assert worktree is not None
        changed = asyncio.run(candidate_changed_paths(self.settings, worktree))
        self.assertEqual(set(changed), {m["path"] for m in result["manifest"]})
        # The worktree index is empty (verification precondition).
        staged = subprocess.run(
            ["git", "-C", str(worktree.path), "diff", "--cached", "--name-only",
             "-z", "--no-renames", "HEAD"],
            capture_output=True,
        ).stdout
        self.assertEqual(staged, b"")
        # The candidate fingerprint reproduces exactly via the verification
        # code path — so verify_development_job will see the same candidate.
        reproduced = asyncio.run(
            _digest_candidate_paths(
                self.settings, self.project, worktree, changed, validate_policy=True
            )
        )
        self.assertEqual(reproduced, result["candidate_fingerprint"])
        # A captured audit row was persisted.
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "captured")
        self.assertEqual(rows[0]["job_id"], result["job_id"])


# --------------------------------------------------------------------------- #
# MCPB-012 defect repair: downstream gates on a captured candidate while the
# canonical source stays dirty (origin-aware clean-source exception)
# --------------------------------------------------------------------------- #


class CapturedCandidateGateTests(CaptureTestCase):
    """Every downstream gate accepts a captured candidate while its source
    checkout remains dirty with the unchanged captured state — and every
    non-captured candidate still fails closed on a dirty source."""

    def _managers(self) -> tuple[Any, Any]:
        verification = VerificationCommitManager(
            self.settings, self.locks, self.jobs, projects=(self.project,)
        )
        review = ReviewManager(
            self.settings,
            self.locks,
            self.jobs,
            ImmediateExecutor(),
            projects=(self.project,),
        )
        return verification, review

    def _capture_dirty(self) -> tuple[dict[str, Any], dict[str, Any]]:
        self._make_dirty()
        before = {
            "status": _status(self.root),
            "head": _head(self.root),
            "agents": (self.root / "AGENTS.md").read_bytes(),
            "notes": (self.root / "notes.md").read_bytes(),
        }
        result = self._capture()
        self.assertEqual(result["status"], "captured")
        return result, before

    def _assert_source_unchanged(self, before: dict[str, Any]) -> None:
        self.assertEqual(_status(self.root), before["status"])
        self.assertEqual(_head(self.root), before["head"])
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), before["agents"])
        self.assertEqual((self.root / "notes.md").read_bytes(), before["notes"])

    def test_18_captured_candidate_passes_read_review_verify_while_source_dirty(
        self,
    ) -> None:
        result, before = self._capture_dirty()
        self._assert_source_unchanged(before)
        verification, review = self._managers()
        job_id = result["job_id"]

        # Inspection gates succeed on the dirty-sourced retained candidate.
        listed = asyncio.run(review.list_candidate_changes(self.project, job_id))
        self.assertEqual(
            {entry["path"] for entry in listed["paths"]},
            {m["path"] for m in result["manifest"]},
        )
        read = asyncio.run(
            review.read_candidate_file(self.project, job_id, "AGENTS.md")
        )
        self.assertEqual(read["content"], "# Agents edited\n")

        # Review recording succeeds and binds the exact candidate state.
        recorded = asyncio.run(
            review.record_candidate_review(
                self.project,
                job_id,
                {"decision": "approved"},
                reviewer_id="human-reviewer",
            )
        )
        self.assertEqual(recorded["status"], "recorded")

        # Fixed verification succeeds against the retained candidate.
        verified = asyncio.run(
            verification.verify(self.project, job_id, result["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        # Throughout, the canonical source stayed dirty and byte-identical.
        self.assertTrue(_status(self.root))
        self._assert_source_unchanged(before)

    def test_19_captured_candidate_commits_while_source_dirty(self) -> None:
        result, before = self._capture_dirty()
        verification, _review = self._managers()
        job_id = result["job_id"]
        verified = asyncio.run(
            verification.verify(self.project, job_id, result["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        committed = asyncio.run(
            verification.commit(
                self.project,
                job_id,
                verified["verification_id"],
                verified["base_head"],
                verified["diff_fingerprint"],
                list(verified["paths"]),
                "MCPB-012 captured candidate",
            )
        )
        self.assertEqual(committed["status"], "committed")
        # The commit happened in the candidate worktree only; the canonical
        # source is still dirty with exactly the captured state.
        self.assertTrue(_status(self.root))
        self._assert_source_unchanged(before)

    def test_20_ordinary_job_denied_under_dirty_source(self) -> None:
        async def exercise() -> None:
            # A retained NON-captured candidate: created and completed while
            # the canonical checkout was clean, then the source becomes dirty.
            started = await self.jobs.start(
                self.project, "make an ordinary candidate", "high"
            )
            snapshot = await wait_for_status(
                self.jobs, self.project, started["job_id"], {"completed"}
            )
            job_id = str(snapshot["job_id"])
            self.assertFalse(
                await self.jobs.is_working_tree_capture_candidate(
                    self.project, job_id
                )
            )
            self._write("AGENTS.md", "# dirty after completion\n")
            verification, _review = self._managers()
            with self.assertRaises(VerificationError) as cm:
                await verification.verify(
                    self.project, job_id, str(snapshot["base_head"])
                )
            self.assertIn("must be clean", str(cm.exception))
            # A brand-new ordinary job cannot even start under the dirty source.
            second = await self.jobs.start(self.project, "must not start", "high")
            failed = await wait_for_status(
                self.jobs, self.project, second["job_id"], {"failed"}
            )
            self.assertIn("must be clean", str(failed.get("error")))

        asyncio.run(exercise())

    def test_21_captured_candidate_denied_after_canonical_head_moves(self) -> None:
        result, before = self._capture_dirty()
        _git(self.root, "commit", "--allow-empty", "-qm", "unrelated head move")
        self.assertNotEqual(_head(self.root), result["base_head"])
        verification, review = self._managers()
        job_id = result["job_id"]
        # Verification is denied after the base-HEAD binding broke ...
        with self.assertRaises(VerificationError) as cm:
            asyncio.run(
                verification.verify(self.project, job_id, result["base_head"])
            )
        self.assertIn("HEAD moved", str(cm.exception))
        # ... and so is candidate inspection ...
        with self.assertRaises(ReviewError):
            asyncio.run(review.list_candidate_changes(self.project, job_id))
        # ... while the captured source state itself is still intact.
        self.assertEqual(_status(self.root), before["status"])
        self.assertEqual((self.root / "AGENTS.md").read_bytes(), before["agents"])

    def test_22_capture_origin_persists_across_job_manager_reload(self) -> None:
        result, before = self._capture_dirty()
        job_id = result["job_id"]
        # A brand-new manager over the same durable state — the restart path:
        # ``_reload_jobs`` rehydrates ``candidate_json`` from the jobs table.
        jobs_reloaded = DevelopmentJobManager(
            self.settings, self.locks, ImmediateExecutor(), projects=(self.project,)
        )
        self.assertTrue(
            asyncio.run(
                jobs_reloaded.is_working_tree_capture_candidate(
                    self.project, job_id
                )
            )
        )
        verification = VerificationCommitManager(
            self.settings, self.locks, jobs_reloaded, projects=(self.project,)
        )
        verified = asyncio.run(
            verification.verify(self.project, job_id, result["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        self._assert_source_unchanged(before)

    def test_23_candidate_drift_remains_denied(self) -> None:
        result, before = self._capture_dirty()
        verification, _review = self._managers()
        job_id = result["job_id"]
        verified = asyncio.run(
            verification.verify(self.project, job_id, result["base_head"])
        )
        self.assertEqual(verified["status"], "passed", verified)
        # Drift the retained candidate AFTER verification ...
        worktree = self.jobs._jobs[job_id].worktree
        assert worktree is not None
        (worktree.path / "AGENTS.md").write_text("# drifted\n", encoding="utf-8")
        # ... the commit gate must deny the mismatch (and quarantine the job).
        with self.assertRaises(VerificationError) as cm:
            asyncio.run(
                verification.commit(
                    self.project,
                    job_id,
                    verified["verification_id"],
                    verified["base_head"],
                    verified["diff_fingerprint"],
                    list(verified["paths"]),
                    "must not commit drifted state",
                )
            )
        self.assertIn("candidate state changed", str(cm.exception))
        # The canonical source still carries the captured dirty state untouched.
        self._assert_source_unchanged(before)


if __name__ == "__main__":
    unittest.main()