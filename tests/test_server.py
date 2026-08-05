import asyncio
import tempfile
import unittest
from pathlib import Path

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
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
                    "capabilities": ["repository_read"],
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


if __name__ == "__main__":
    unittest.main()
