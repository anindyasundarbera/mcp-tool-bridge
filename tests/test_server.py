import asyncio
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.models import Effort, ProjectPolicy
from local_dev_mcp.registry import ProjectRegistry
from local_dev_mcp.server import create_server

from tests.support import initialize_git_repository


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
                    ],
                    "markdown_write_roots": ["docs"],
                }
            )
            server = create_server(
                Settings.from_env(
                    {"PROJECTS_CONFIG_PATH": str(base / "projects.json")}
                ),
                ProjectRegistry({"test-project": project}),
            )
            tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

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

    def test_writer_and_claude_share_the_project_modifying_lock(self) -> None:
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
                effort: Effort,
            ) -> dict[str, object]:
                self.entered.set()
                return {"operation": "claude"}

        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "repo"
                initialize_git_repository(root)
                (root / "docs").mkdir()
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
                        {"PROJECTS_CONFIG_PATH": str(base / "projects.json")}
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
                claude_call = asyncio.create_task(
                    server.call_tool(
                        "send_to_local_dev",
                        {
                            "project_id": "test-project",
                            "text": "approved work",
                            "effort": "high",
                        },
                    )
                )
                await asyncio.sleep(0.05)
                self.assertFalse(observing_executor.entered.is_set())

                blocking_writer.release.set()
                await asyncio.gather(writer_call, claude_call)
                self.assertTrue(observing_executor.entered.is_set())

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
