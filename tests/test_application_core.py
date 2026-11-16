"""Tests for the transport-neutral governance application core (A1.3.1)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from local_dev_mcp.application import (
    GovernanceApplication,
    RequestContext,
)
from local_dev_mcp.config import Settings
from local_dev_mcp.models import Effort, ProjectCapability, ProjectPolicy
from local_dev_mcp.review import ReviewError
from local_dev_mcp import store
from tests.support import initialize_git_repository, project_payload, write_registry


class ApplicationAstTests(unittest.TestCase):
    """AST validation to ensure zero FastMCP dependency in application.py."""

    def test_application_py_has_no_fastmcp_imports(self) -> None:
        source_path = (
            Path(__file__).parent.parent
            / "src"
            / "local_dev_mcp"
            / "application.py"
        )
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        "fastmcp",
                        alias.name.lower(),
                        f"Disallowed import of fastmcp found in {source_path}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(
                        "fastmcp",
                        node.module.lower(),
                        f"Disallowed from fastmcp import found in {source_path}",
                    )
        self.assertNotIn("from fastmcp", source)
        self.assertNotIn("import fastmcp", source)


class RequestContextTests(unittest.TestCase):
    """Tests for RequestContext defaults and immutability."""

    def test_request_context_defaults(self) -> None:
        ctx = RequestContext()
        self.assertEqual(ctx.caller_id, "local")
        self.assertEqual(ctx.transport, "direct")
        self.assertIsNone(ctx.correlation_id)

    def test_request_context_custom_values(self) -> None:
        ctx = RequestContext(
            caller_id="agent-xyz",
            transport="mcp-http",
            correlation_id="corr-999",
        )
        self.assertEqual(ctx.caller_id, "agent-xyz")
        self.assertEqual(ctx.transport, "mcp-http")
        self.assertEqual(ctx.correlation_id, "corr-999")

    def test_request_context_frozen(self) -> None:
        ctx = RequestContext()
        with self.assertRaises(Exception):
            ctx.caller_id = "other"  # type: ignore[misc]


class ImmediateTestExecutor:
    """Fake ClaudeExecutor that performs candidate modifications immediately."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        assert root is not None
        self.calls.append(
            {
                "root": root,
                "text": specification,
                "effort": effort,
                "resume_session_id": resume_session_id,
            }
        )
        (root / "candidate.txt").write_text(
            f"candidate update {len(self.calls)}\n", encoding="utf-8"
        )
        return {
            "status": "completed",
            "project_id": project.id,
            "result": f"changed {root}",
            "session_id": "bridge-session-test-1",
            "repository_after": {"clean": False, "working_tree": "?? candidate.txt"},
        }


class ApplicationEndToEndLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end transport-neutral lifecycle test calling GovernanceApplication directly."""

    async def test_full_governance_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repo_dir = base / "repo"
            initialize_git_repository(repo_dir)

            docs_dir = repo_dir / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            guide_path = docs_dir / "guide.md"
            guide_path.write_text("# Documentation Guide\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_dir), "add", "docs/guide.md"], check=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "Add docs"], check=True)

            policy_data = project_payload(
                repo_dir,
                "test-project",
                capabilities=[
                    "repository_read",
                    "markdown_write",
                    "claude_edit",
                    "candidate_review",
                    "verification_commit",
                    "job_introspection",
                    "policy_introspection",
                ],
                markdown_write_roots=["docs"],
                commit_roots=["."],
                verification_commands=[
                    {"id": "noop", "argv": ["python3", "-c", "import sys; sys.exit(0)"]}
                ],
                requires_review=True,
            )
            write_registry(base / "projects.json", [policy_data])

            app_settings = Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(base / "worktrees"),
                    "STATE_ROOT": str(base / "state"),
                }
            )
            fake_executor = ImmediateTestExecutor()

            ctx = RequestContext(caller_id="tester", transport="direct", correlation_id="c-001")
            app = GovernanceApplication(
                settings=app_settings,
                executor=fake_executor,  # type: ignore[arg-type]
            )

            try:
                # 1. Project discovery & status
                projects_res = await app.list_allowed_projects(context=ctx)
                self.assertEqual(len(projects_res["projects"]), 1)
                self.assertEqual(projects_res["projects"][0]["project_id"], "test-project")

                status_res = await app.get_project_status("test-project", context=ctx)
                self.assertEqual(status_res["project_id"], "test-project")
                self.assertTrue(status_res["repository"]["clean"])

                policy_view = await app.get_effective_project_policy("test-project", context=ctx)
                self.assertIn("policy_fingerprint", policy_view)
                self.assertEqual(policy_view["project_id"], "test-project")

                # Subservice access verification
                sub_projects = await app.inspection.list_allowed_projects(context=ctx)
                self.assertEqual(sub_projects, projects_res)

                # 2. Markdown operations
                md_list = await app.list_project_markdown("test-project", context=ctx)
                self.assertTrue(any(p["path"] == "README.md" for p in md_list["files"]))

                md_read = await app.read_project_markdown("test-project", "README.md", context=ctx)
                self.assertIn("# Test", md_read["content"])

                md_outline = await app.get_markdown_outline("test-project", "README.md", context=ctx)
                self.assertEqual(len(md_outline["headings"]), 1)

                write_res = await app.write_project_markdown(
                    "test-project",
                    "docs/new_file.md",
                    "# New Page\n",
                    write_mode="create",
                    context=ctx,
                )
                self.assertIn("after_sha256", write_res)
                subprocess.run(["git", "-C", str(repo_dir), "add", "docs/new_file.md"], check=True)
                subprocess.run(["git", "-C", str(repo_dir), "commit", "-qm", "Commit new markdown"], check=True)

                # 3. Development job delegation
                send_res = await app.send_to_local_dev(
                    "test-project",
                    "Add candidate.txt with test content",
                    context=ctx,
                )
                job_id = send_res["job_id"]
                self.assertEqual(len(job_id), 32)

                # Wait for job completion
                job_snap = None
                for _ in range(200):
                    job_snap = await app.get_development_job("test-project", job_id, context=ctx)
                    if job_snap["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)

                self.assertIsNotNone(job_snap)
                assert job_snap is not None
                self.assertEqual(job_snap["status"], "completed")

                jobs_listing = await app.list_development_jobs("test-project", context=ctx)
                self.assertTrue(any(j["job_id"] == job_id for j in jobs_listing["jobs"]))

                # 4. Review, verification, and commit
                changes = await app.list_candidate_changes("test-project", job_id, context=ctx)
                self.assertTrue(any(c["path"] == "candidate.txt" for c in changes["paths"]))

                cand_file = await app.read_candidate_file(
                    "test-project", job_id, "candidate.txt", context=ctx
                )
                self.assertIn("candidate update", cand_file["content"])

                base_head = job_snap["base_head"]
                verify_res = await app.verify_development_job(
                    "test-project", job_id, base_head, context=ctx
                )
                self.assertEqual(verify_res["conclusion"], "PASS")
                verification_id = verify_res["verification_id"]
                fingerprint = verify_res["diff_fingerprint"]
                paths = verify_res["paths"]

                review_res = await app.record_candidate_review(
                    "test-project",
                    job_id,
                    {"summary": "verified candidate", "verdict": "APPROVE"},
                    reviewer_id="human-reviewer",
                    context=ctx,
                )
                review_id = review_res["review_id"]

                commit_res = await app.commit_verified_development_job(
                    "test-project",
                    job_id,
                    verification_id,
                    base_head,
                    fingerprint,
                    paths,
                    "feat: test candidate commit",
                    review_id=review_id,
                    context=ctx,
                )
                self.assertIn("commit_sha", commit_res)
                self.assertEqual(len(commit_res["commit_sha"]), 40)

            finally:
                # 5. Clean close
                app.close()


class ApplicationMockedTests(unittest.IsolatedAsyncioTestCase):
    """Component-level tests with mocked domain managers."""

    async def asyncSetUp(self) -> None:
        self.project = ProjectPolicy.model_validate(
            {
                "id": "mock-project",
                "display_name": "Mock Project",
                "description": "Mocked",
                "root": "/tmp/mock-project",
                "enabled": True,
                "require_git": False,
                "capabilities": [cap.value for cap in ProjectCapability],
                "markdown_write_roots": ["docs"],
                "commit_roots": ["."],
                "verification_commands": [
                    {"id": "core", "argv": ["python3", "-c", "import sys; sys.exit(0)"]}
                ],
                "runtime_verification_commands": [
                    {"id": "health", "argv": ["python3", "-c", "import sys; sys.exit(0)"]}
                ],
                "working_tree_capture_scope": {
                    "include_paths": ["docs/**"],
                    "protected_exclude_paths": [".env"],
                },
                "default_effort": "high",
                "allow_ultracode": True,
                "requires_review": False,
            }
        )
        self.registry = MagicMock()
        self.registry.projects.return_value = (self.project,)
        self.registry.resolve.return_value = self.project
        self.registry.list_public.return_value = [{"id": "mock-project"}]

        self.executor = MagicMock()
        self.reader = MagicMock()
        self.writer = MagicMock()
        self.locks = MagicMock()
        self.jobs = MagicMock()
        self.verification = MagicMock()
        self.review = MagicMock()
        self.publication = MagicMock()
        self.sync = MagicMock()
        self.evidence = MagicMock()
        self.pr = MagicMock()
        self.canonicalization = MagicMock()
        self.runtime = MagicMock()
        self.capture = MagicMock()
        self.requirements = MagicMock()

        self.reader.list_markdown = AsyncMock(return_value={"paths": []})
        self.reader.search = AsyncMock(return_value={"matches": []})
        self.reader.read_markdown = AsyncMock(return_value={"content": "# Doc"})
        self.reader.read_range = AsyncMock(return_value={"lines": []})
        self.reader.outline = AsyncMock(return_value={"headings": []})
        self.reader.metadata = AsyncMock(return_value={"sha256": "0" * 64})

        self.writer.write = AsyncMock(return_value={"sha256": "1" * 64})
        self.locks.for_project = AsyncMock(return_value=AsyncMock())

        self.jobs.start = AsyncMock(return_value={"job_id": "a" * 32})
        self.jobs.get = AsyncMock(return_value={"status": "completed"})
        self.jobs.resume = AsyncMock(return_value={"status": "running"})
        self.jobs.cancel = AsyncMock(return_value={"status": "cancelled"})
        self.jobs.pause = AsyncMock(return_value={"status": "paused"})
        self.jobs.extend_budget = AsyncMock(return_value={"granted": 1})
        self.jobs.narrow_scope = AsyncMock(return_value={"scope": []})
        self.jobs.replace_worker = AsyncMock(return_value={"status": "running"})
        self.jobs.discard_candidate = AsyncMock(return_value={"discarded": True})
        self.jobs.revoke_authorization = AsyncMock(return_value={"revoked": True})
        self.jobs.require_job_capability = AsyncMock(return_value=None)
        self.jobs.list_jobs = AsyncMock(return_value={"jobs": []})
        req_summary_mgr = MagicMock()
        req_summary_mgr.requirements_covered_summary = AsyncMock(return_value={"normative": 0})
        self.jobs._requirements_manager = MagicMock(return_value=req_summary_mgr)

        self.verification.verify = AsyncMock(return_value={"verification_id": "v" * 32})
        self.verification.replay = AsyncMock(return_value={"reproduces": True})
        self.verification.commit = AsyncMock(return_value={"commit_sha": "c" * 40})

        self.review.list_candidate_changes = AsyncMock(return_value={"changes": []})
        self.review.read_candidate_file = AsyncMock(return_value={"content": "data"})
        self.review.record_candidate_review = AsyncMock(return_value={"review_id": "r" * 32})
        self.review.run_independent_review = AsyncMock(return_value={"review_id": "r" * 32})
        self.review.get_candidate_review = AsyncMock(return_value={"verdict": {}})
        self.review.validate_for_commit = AsyncMock(return_value=None)
        self.review.mark_used = AsyncMock(return_value=None)

        self.publication.publish = AsyncMock(return_value={"branch": "feat/x", "commit_sha": "c" * 40})
        self.sync.sync = AsyncMock(return_value={"synced": True})
        self.sync.check_for_updates = AsyncMock(return_value={"updates_available": False})
        self.evidence.export_bundle = AsyncMock(return_value={"bundle_sha256": "e" * 64})
        self.pr.open_pull_request = AsyncMock(return_value={"pr_number": 42})
        self.canonicalization.resolve = AsyncMock(return_value={"verdict": "CANONICAL"})
        self.runtime.run = AsyncMock(return_value={"result": "PASS"})
        self.capture.preflight = AsyncMock(return_value={"working_tree_fingerprint": "w" * 64})
        self.capture.capture = AsyncMock(return_value={"job_id": "c" * 32})

        self.state_db = sqlite3.connect(":memory:")
        self.settings = Settings.from_env(
            {
                "PROJECTS_CONFIG_PATH": "/tmp/mock-projects.json",
                "JOB_WORKTREE_ROOT": "/tmp/mock-worktrees",
            }
        )

        self.app = GovernanceApplication(
            settings=self.settings,
            registry=self.registry,
            executor=self.executor,
            reader=self.reader,
            writer=self.writer,
            locks=self.locks,
            jobs=self.jobs,
            verification=self.verification,
            review=self.review,
            publication=self.publication,
            sync=self.sync,
            evidence=self.evidence,
            pr=self.pr,
            canonicalization=self.canonicalization,
            runtime=self.runtime,
            capture=self.capture,
            requirements=self.requirements,
            state_db=self.state_db,
        )

    async def asyncTearDown(self) -> None:
        try:
            self.app.close()
        except Exception:
            pass
        try:
            self.state_db.close()
        except Exception:
            pass

    async def test_subservices_and_top_level_operations_all_available(self) -> None:
        """Verify that every sub-service is wired and accessible."""
        self.assertIsNotNone(self.app.inspection)
        self.assertIsNotNone(self.app.repository)
        self.assertIsNotNone(self.app.jobs)
        self.assertIsNotNone(self.app.verification)
        self.assertIsNotNone(self.app.review)
        self.assertIsNotNone(self.app.publication)
        self.assertIsNotNone(self.app.capture)
        self.assertIsNotNone(self.app.requirements)

        # Inspection
        res = await self.app.inspection.list_allowed_projects()
        self.assertEqual(res, {"projects": [{"id": "mock-project"}]})

        # Repository
        await self.app.repository.list_project_markdown("mock-project")
        self.reader.list_markdown.assert_awaited_once()

        # Jobs
        await self.app.jobs.send_to_local_dev("mock-project", "spec")
        self.jobs.start.assert_awaited_once()

        # Verification
        await self.app.verification.verify_development_job("mock-project", "a" * 32, "h" * 40)
        self.verification.verify.assert_awaited_once()

        # Review
        await self.app.review.list_candidate_changes("mock-project", "a" * 32)
        self.review.list_candidate_changes.assert_awaited_once()

        # Publication
        await self.app.publication.sync_canonical_checkout("mock-project", "h" * 40, "t" * 40)
        self.sync.sync.assert_awaited_once()

        # Capture
        await self.app.capture.preflight_working_tree_capture("mock-project", ["docs/**"], [])
        self.capture.preflight.assert_awaited_once()

    async def test_unowned_state_db_is_not_closed(self) -> None:
        """When state_db is passed externally, app.close() does not close it."""
        self.assertFalse(self.app._owns_db)
        self.assertFalse(self.app.owns_state_db)
        self.app.close()
        # Verify state_db connection is still alive by executing a query
        cursor = self.state_db.execute("SELECT 1")
        self.assertEqual(cursor.fetchone(), (1,))

    async def test_owned_state_db_is_closed(self) -> None:
        """When state_db is owned, app.close() closes it."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "test.db"
            conn = store.open_state_db(db_path)
            app = GovernanceApplication(
                settings=self.settings,
                registry=self.registry,
                state_db=conn,
            )
            # Simulate app owning the db
            app._owns_db = True
            app.owns_state_db = True
            app.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    async def test_context_managers_call_close(self) -> None:
        """Synchronous and asynchronous context manager protocols."""
        with patch.object(self.app, "close") as mock_close:
            with self.app as entered:
                self.assertIs(entered, self.app)
            mock_close.assert_called_once()

        with patch.object(self.app, "close") as mock_close:
            async with self.app as entered:
                self.assertIs(entered, self.app)
            mock_close.assert_called_once()

    async def test_reader_tools_resolve_repository_read(self) -> None:
        await self.app.list_project_markdown("mock-project")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

        await self.app.search_project_markdown("mock-project", "test")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

        await self.app.read_project_markdown("mock-project", "README.md")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

        await self.app.read_project_file_range("mock-project", "README.md", 1, 5)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

        await self.app.get_markdown_outline("mock-project", "README.md")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

        await self.app.get_project_file_metadata("mock-project", "README.md")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPOSITORY_READ)

    async def test_writer_tool_resolves_markdown_write(self) -> None:
        await self.app.write_project_markdown("mock-project", "docs/test.md", "# Test")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.MARKDOWN_WRITE)

    async def test_jobs_tools_resolve_claude_edit_and_require_job_capability(self) -> None:
        await self.app.send_to_local_dev("mock-project", "task text")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CLAUDE_EDIT)

        await self.app.get_development_job("mock-project", "a" * 32)
        self.jobs.require_job_capability.assert_awaited_with(
            self.project, "a" * 32, ProjectCapability.CLAUDE_EDIT
        )

        await self.app.resume_development_job("mock-project", "a" * 32, "resume text")
        self.jobs.require_job_capability.assert_awaited_with(
            self.project, "a" * 32, ProjectCapability.CLAUDE_EDIT
        )

        await self.app.cancel_development_job("mock-project", "a" * 32)
        self.jobs.require_job_capability.assert_awaited_with(
            self.project, "a" * 32, ProjectCapability.CLAUDE_EDIT
        )

    async def test_control_surface_resolves_capabilities(self) -> None:
        controls = [
            ("pause_development_job", ProjectCapability.PAUSE_DEVELOPMENT_JOB, ("mock-project", "a" * 32), {}),
            ("extend_budget", ProjectCapability.EXTEND_BUDGET, ("mock-project", "a" * 32), {"slices": 1}),
            ("narrow_scope", ProjectCapability.NARROW_SCOPE, ("mock-project", "a" * 32, ["docs"]), {}),
            ("replace_worker", ProjectCapability.REPLACE_WORKER, ("mock-project", "a" * 32, "direction"), {}),
            ("discard_candidate", ProjectCapability.DISCARD_CANDIDATE, ("mock-project", "a" * 32), {}),
            ("revoke_authorization", ProjectCapability.REVOKE_AUTHORIZATION, ("mock-project", "auth-1"), {}),
        ]
        for method_name, capability, args, kwargs in controls:
            with self.subTest(control=method_name):
                self.registry.resolve.reset_mock()
                method = getattr(self.app, method_name)
                await method(*args, **kwargs)
                self.registry.resolve.assert_called_with("mock-project", capability)

    async def test_verification_and_review_gate_resolutions(self) -> None:
        await self.app.verify_development_job("mock-project", "a" * 32, "h" * 40)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.VERIFICATION_COMMIT)
        self.jobs.require_job_capability.assert_awaited_with(
            self.project, "a" * 32, ProjectCapability.VERIFICATION_COMMIT
        )

        await self.app.replay_verification("mock-project", "v" * 32)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.REPLAY_VERIFICATION)

        await self.app.list_candidate_changes("mock-project", "a" * 32)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CANDIDATE_REVIEW)

        await self.app.read_candidate_file("mock-project", "a" * 32, "file.py")
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CANDIDATE_REVIEW)

        await self.app.record_candidate_review("mock-project", "a" * 32, {"ok": True})
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CANDIDATE_REVIEW)

        await self.app.run_independent_review("mock-project", "a" * 32)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CANDIDATE_REVIEW)

        await self.app.get_candidate_review("mock-project", "a" * 32, "r" * 32)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.CANDIDATE_REVIEW)

    async def test_commit_requires_review_raises_review_error_mapped_to_value_error(self) -> None:
        self.project = self.project.model_copy(update={"requires_review": True})
        self.registry.resolve.return_value = self.project
        with self.assertRaises(ValueError) as ctx:
            await self.app.commit_verified_development_job(
                "mock-project",
                "a" * 32,
                "v" * 32,
                "h" * 40,
                "f" * 64,
                ["f.py"],
                "msg",
                review_id=None,
            )
        self.assertIn("review_id is required", str(ctx.exception))

    async def test_publication_and_sync_capabilities(self) -> None:
        await self.app.push_detached_development_commit(
            "mock-project", "a" * 32, "c" * 40, "feat/branch", "h" * 40, "f" * 64
        )
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.PUSH_DEVELOPMENT_COMMIT)

        await self.app.open_pull_request(
            "mock-project", "a" * 32, "h" * 40, "f" * 64, "PR Title"
        )
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.OPEN_PULL_REQUEST)

        await self.app.export_evidence_bundle("mock-project", "a" * 32, "f" * 64)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.EXPORT_EVIDENCE_BUNDLE)

        await self.app.sync_canonical_checkout("mock-project", "h" * 40, "t" * 40)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.SYNC_CANONICAL_CHECKOUT)

        await self.app.prepare_canonical_for_work("mock-project", pull=False)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.SYNC_CANONICAL_CHECKOUT)

        await self.app.prepare_canonical_for_work("mock-project", pull=True)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.SYNC_CANONICAL_CHECKOUT)

    async def test_runtime_and_capture_capabilities(self) -> None:
        await self.app.run_runtime_verification("mock-project", "health", "h" * 40)
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.RUNTIME_VERIFICATION)

        await self.app.preflight_working_tree_capture("mock-project", ["docs/**"], [".env"])
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE)

        await self.app.working_tree_candidate_capture("mock-project", ["docs/**"], [".env"])
        self.registry.resolve.assert_called_with("mock-project", ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE)

        mock_exec = AsyncMock(return_value={"result": "ok"})
        with patch.object(self.app.open_hands_manager, "execute_action", new=mock_exec):
            await self.app.standalone_openhands_action("mock-project", {"action": "run"})
        self.registry.resolve.assert_called_with(
            "mock-project", ProjectCapability.GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION
        )


if __name__ == "__main__":
    unittest.main()
