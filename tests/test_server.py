import asyncio
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectRegistry
from local_dev_mcp.server import create_server

from tests.support import initialize_git_repository
from tests.test_sync import add_origin, advance_origin, init_repo, rev


class ServerSchemaTests(unittest.TestCase):
    def test_exposes_reader_tools_with_read_only_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            initialize_git_repository(root)
            project = ProjectPolicy.model_validate(
                {
                    "id": "test-project",
                    "display_name": "Test",
                    "description": "Test",
                    "root": str(root),
                    "capabilities": [
                        "repository_read",
                        "markdown_write",
                        "claude_edit",
                        "verification_commit",
                    ],
                    "markdown_write_roots": ["docs"],
                    "commit_roots": ["."],
                    "verification_commands": [
                        {"id": "core", "argv": ["{python}", "-m", "unittest"]}
                    ],
                }
            )
            server = create_server(
                Settings.from_env(
                    {"PROJECTS_CONFIG_PATH": str(base / "projects.json")}
                ),
                ProjectRegistry({"test-project": project}),
            )
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

        self.assertEqual(len(tools), 28)
        reader_tools = {
            "list_project_markdown",
            "search_project_markdown",
            "read_project_markdown",
            "read_project_file_range",
            "get_markdown_outline",
            "get_project_file_metadata",
        }
        self.assertTrue(reader_tools.issubset(tools))
        for name in reader_tools:
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
            self.assertFalse(tools[name].annotations.openWorldHint)

        range_schema = tools["read_project_file_range"].parameters
        self.assertEqual(range_schema["properties"]["start_line"]["minimum"], 1)
        self.assertIn("expected_sha256", range_schema["properties"])

        writer = tools["write_project_markdown"]
        self.assertFalse(writer.annotations.readOnlyHint)
        self.assertTrue(writer.annotations.destructiveHint)
        self.assertFalse(writer.annotations.idempotentHint)
        self.assertFalse(writer.annotations.openWorldHint)
        writer_schema = writer.parameters
        self.assertEqual(writer_schema["properties"]["write_mode"]["default"], "create")
        self.assertEqual(
            set(writer_schema["properties"]["write_mode"]["enum"]),
            {"create", "update"},
        )
        self.assertIn("expected_sha256", writer_schema["properties"])
        self.assertIn(
            "Required for update",
            writer_schema["properties"]["expected_sha256"]["description"],
        )

        lifecycle = {
            "send_to_local_dev",
            "get_development_job",
            "resume_development_job",
            "cancel_development_job",
        }
        self.assertTrue(lifecycle.issubset(tools))
        self.assertFalse(tools["send_to_local_dev"].annotations.readOnlyHint)
        self.assertTrue(tools["send_to_local_dev"].annotations.openWorldHint)
        self.assertTrue(tools["get_development_job"].annotations.readOnlyHint)
        self.assertFalse(tools["get_development_job"].annotations.idempotentHint)
        self.assertFalse(tools["resume_development_job"].annotations.readOnlyHint)
        self.assertTrue(tools["resume_development_job"].annotations.openWorldHint)
        self.assertTrue(tools["cancel_development_job"].annotations.idempotentHint)
        self.assertFalse(tools["cancel_development_job"].annotations.openWorldHint)
        for name in (
            "get_development_job",
            "resume_development_job",
            "cancel_development_job",
        ):
            schema = tools[name].parameters
            self.assertEqual(
                schema["properties"]["job_id"]["pattern"], r"^[0-9a-f]{32}$"
            )
        self.assertNotIn(
            "session_id", tools["resume_development_job"].parameters["properties"]
        )
        self.assertEqual(
            tools["resume_development_job"].parameters["properties"]["effort"]["default"],
            "high",
        )

        verification_tools = {
            "verify_development_job",
            "commit_verified_development_job",
        }
        self.assertTrue(verification_tools.issubset(tools))
        for name in verification_tools:
            self.assertFalse(tools[name].annotations.readOnlyHint)
            self.assertTrue(tools[name].annotations.destructiveHint)
            self.assertFalse(tools[name].annotations.idempotentHint)
            self.assertEqual(
                tools[name].parameters["properties"]["job_id"]["pattern"],
                r"^[0-9a-f]{32}$",
            )
            self.assertEqual(
                tools[name].parameters["properties"]["expected_head"]["pattern"],
                r"^[0-9a-f]{40}$",
            )
        self.assertTrue(tools["verify_development_job"].annotations.openWorldHint)
        self.assertFalse(
            tools["commit_verified_development_job"].annotations.openWorldHint
        )
        commit_schema = tools["commit_verified_development_job"].parameters
        self.assertEqual(
            commit_schema["properties"]["verification_id"]["pattern"],
            r"^[0-9a-f]{32}$",
        )
        self.assertEqual(
            commit_schema["properties"]["expected_diff_fingerprint"]["pattern"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(commit_schema["properties"]["paths"]["minItems"], 1)
        self.assertEqual(commit_schema["properties"]["paths"]["maxItems"], 500)
        self.assertNotIn("command", tools["verify_development_job"].parameters["properties"])

        review_tools = {
            "list_candidate_changes",
            "read_candidate_file",
            "record_candidate_review",
            "run_independent_review",
            "get_candidate_review",
        }
        self.assertTrue(review_tools.issubset(tools))
        for name in (
            "list_candidate_changes",
            "read_candidate_file",
            "get_candidate_review",
        ):
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
            self.assertFalse(tools[name].annotations.openWorldHint)
            self.assertEqual(
                tools[name].parameters["properties"]["job_id"]["pattern"],
                r"^[0-9a-f]{32}$",
            )
        self.assertFalse(tools["record_candidate_review"].annotations.readOnlyHint)
        self.assertFalse(tools["record_candidate_review"].annotations.destructiveHint)
        self.assertFalse(tools["record_candidate_review"].annotations.openWorldHint)
        self.assertFalse(tools["run_independent_review"].annotations.readOnlyHint)
        self.assertTrue(tools["run_independent_review"].annotations.openWorldHint)
        commit_schema = tools["commit_verified_development_job"].parameters
        self.assertIn("review_id", commit_schema["properties"])
        self.assertIn(
            "requires_review",
            commit_schema["properties"]["review_id"]["description"],
        )

        push = tools["push_detached_development_commit"]
        self.assertFalse(push.annotations.readOnlyHint)
        self.assertTrue(push.annotations.destructiveHint)
        self.assertTrue(push.annotations.idempotentHint)
        self.assertTrue(push.annotations.openWorldHint)
        push_schema = push.parameters
        for field_name, pattern in (
            ("job_id", r"^[0-9a-f]{32}$"),
            ("commit_sha", r"^[0-9a-f]{40}$"),
            ("expected_base_head", r"^[0-9a-f]{40}$"),
            ("expected_diff_fingerprint", r"^[0-9a-f]{64}$"),
        ):
            self.assertEqual(push_schema["properties"][field_name]["pattern"], pattern)
        self.assertEqual(push_schema["properties"]["branch_name"]["minLength"], 1)
        self.assertEqual(push_schema["properties"]["branch_name"]["maxLength"], 200)

        # M-AL-01 read-only introspection tools (dormant by default; opt-in via
        # the job_introspection / policy_introspection / canonical_history_read
        # capabilities). They are registered regardless of the project's
        # capabilities (authorization is enforced at call time via resolve).
        introspection_tools = {
            "list_development_jobs",
            "get_effective_project_policy",
            "resolve_candidate_canonicalization",
        }
        self.assertTrue(introspection_tools.issubset(tools))
        for name in introspection_tools:
            self.assertTrue(tools[name].annotations.readOnlyHint)
            self.assertFalse(tools[name].annotations.destructiveHint)
            self.assertTrue(tools[name].annotations.idempotentHint)
        # list_development_jobs / get_effective_project_policy are local-only;
        # canonical history reconciliation reads origin, so it is openWorld.
        self.assertFalse(tools["list_development_jobs"].annotations.openWorldHint)
        self.assertFalse(
            tools["get_effective_project_policy"].annotations.openWorldHint
        )
        self.assertTrue(
            tools["resolve_candidate_canonicalization"].annotations.openWorldHint
        )
        list_schema = tools["list_development_jobs"].parameters
        self.assertEqual(
            set(list_schema["properties"]["scope"]["enum"]),
            {"all", "active", "terminal"},
        )
        self.assertEqual(list_schema["properties"]["scope"]["default"], "all")
        self.assertEqual(list_schema["properties"]["limit"]["default"], 50)
        self.assertEqual(list_schema["properties"]["limit"]["minimum"], 0)
        resolve_schema = tools["resolve_candidate_canonicalization"].parameters
        self.assertEqual(
            resolve_schema["properties"]["commit_sha"]["pattern"],
            r"^[0-9a-fA-F]{40}$",
        )

    def test_writer_and_development_jobs_share_the_project_modifying_lock(self) -> None:
        class BlockingWriter:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def write(
                self,
                project: ProjectPolicy,
                path: str,
                content: str,
                write_mode: str,
                expected_sha256: str | None,
            ) -> dict[str, object]:
                self.entered.set()
                await self.release.wait()
                return {"operation": "writer"}

        class ObservingExecutor:
            def __init__(self) -> None:
                self.entered = asyncio.Event()

            async def execute(
                self,
                project: ProjectPolicy,
                text: str,
                effort: str,
                *,
                root: Path | None = None,
                resume_session_id: str | None = None,
                max_turns: int | None = None,
            ) -> dict[str, object]:
                self.entered.set()
                return {"status": "completed", "result": "done"}

        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "repo"
                initialize_git_repository(root)
                project = ProjectPolicy.model_validate(
                    {
                        "id": "test-project",
                        "display_name": "Test",
                        "description": "Test",
                        "root": str(root),
                        "capabilities": [
                            "repository_read",
                            "markdown_write",
                            "claude_edit",
                        ],
                        "markdown_write_roots": ["docs"],
                    }
                )
                blocking_writer = BlockingWriter()
                observing_executor = ObservingExecutor()
                server = create_server(
                    Settings.from_env(
                        {
                            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
                            "JOB_WORKTREE_ROOT": str(base / "jobs"),
                        }
                    ),
                    ProjectRegistry({"test-project": project}),
                    writer=blocking_writer,  # type: ignore[arg-type]
                    executor=observing_executor,  # type: ignore[arg-type]
                )

                writer_call = asyncio.create_task(
                    server.call_tool(
                        "write_project_markdown",
                        {
                            "project_id": "test-project",
                            "path": "docs/new.md",
                            "content": "content",
                        },
                    )
                )
                await asyncio.wait_for(blocking_writer.entered.wait(), timeout=1)
                await server.call_tool(
                    "send_to_local_dev",
                    {
                        "project_id": "test-project",
                        "text": "approved work",
                        "effort": "high",
                    },
                )
                await asyncio.sleep(0.05)
                self.assertFalse(observing_executor.entered.is_set())

                blocking_writer.release.set()
                await writer_call
                await asyncio.wait_for(observing_executor.entered.wait(), timeout=2)

        asyncio.run(exercise())


class PrepareCanonicalForWorkTests(unittest.TestCase):
    """Server-level check-then-pull flow for the pre-work update tool."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.base_head = init_repo(self.root)
        self.bare = self.base / "remote.git"
        add_origin(self.root, self.bare)
        self.project = ProjectPolicy.model_validate(
            {
                "id": "test-project",
                "display_name": "Test",
                "description": "Test",
                "root": str(self.root),
                "capabilities": ["repository_read", "sync_canonical_checkout"],
                "protected_paths": [".git", ".env"],
            }
        )
        self.server = create_server(
            Settings.from_env(
                {
                    "PROJECTS_CONFIG_PATH": str(self.base / "projects.json"),
                    "JOB_WORKTREE_ROOT": str(self.base / "jobs"),
                }
            ),
            ProjectRegistry({"test-project": self.project}),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_check_reports_updates_then_pull_fast_forwards(self) -> None:
        async def exercise() -> None:
            target = advance_origin(self.bare, self.base, "server-merge")
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

            check = await self.server.call_tool(
                "prepare_canonical_for_work",
                {"project_id": "test-project", "pull": False},
            )
            check_data = check.structured_content
            self.assertEqual(check_data["state"], "updates_available")
            self.assertTrue(check_data["updates_available"])
            self.assertEqual(check_data["remote_tip"], target)
            self.assertEqual(check_data["behind_by"], 1)

            pulled = await self.server.call_tool(
                "prepare_canonical_for_work",
                {"project_id": "test-project", "pull": True},
            )
            pulled_data = pulled.structured_content
            self.assertEqual(pulled_data["status"], "synchronized")
            self.assertEqual(pulled_data["to_head"], target)
            self.assertEqual(rev(self.root, "HEAD"), target)

        asyncio.run(exercise())

    def test_pull_when_up_to_date_is_idempotent(self) -> None:
        async def exercise() -> None:
            pulled = await self.server.call_tool(
                "prepare_canonical_for_work",
                {"project_id": "test-project", "pull": True},
            )
            pulled_data = pulled.structured_content
            self.assertEqual(pulled_data["status"], "already_synchronized")
            self.assertTrue(pulled_data["idempotent"])
            self.assertEqual(rev(self.root, "HEAD"), self.base_head)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
