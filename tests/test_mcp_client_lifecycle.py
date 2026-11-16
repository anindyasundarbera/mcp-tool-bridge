"""End-to-end client lifecycle tests using FastMCP Client against local_dev_mcp."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest

from fastmcp import Client
from fastmcp.exceptions import ToolError

from local_dev_mcp.config import Settings
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectRegistry
from local_dev_mcp.server import create_server
from tests.support import initialize_git_repository


class McpClientLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.repo_dir = base / "repo"
        initialize_git_repository(self.repo_dir)

        # Seed documentation files
        docs_dir = self.repo_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "guide.md").write_text(
            "# User Guide\nWelcome to the MCP bridge.\nSection 1.\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo_dir), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-qm", "Seed documentation"],
            check=True,
        )
        self.initial_head = (
            subprocess.check_output(["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"])
            .decode("utf-8")
            .strip()
        )

        self.project_policy = ProjectPolicy.model_validate(
            {
                "id": "lifecycle-project",
                "display_name": "Lifecycle Project",
                "description": "Project for client lifecycle testing",
                "root": str(self.repo_dir),
                "capabilities": [
                    "repository_read",
                    "claude_edit",
                    "policy_introspection",
                    "job_introspection",
                ],
                "markdown_write_roots": ["docs"],
            }
        )

        self.server = create_server(
            Settings.from_env(
                {"PROJECTS_CONFIG_PATH": str(base / "projects.json")}
            ),
            ProjectRegistry({"lifecycle-project": self.project_policy}),
        )

    def tearDown(self) -> None:
        if hasattr(self.server, "close"):
            self.server.close()
        self.temp_dir.cleanup()

    def test_client_handshake_and_tool_discovery(self) -> None:
        async def run_handshake() -> None:
            async with Client(self.server) as client:
                tools = await client.list_tools()
                self.assertEqual(len(tools), 39)
                tool_map = {t.name: t for t in tools}

                expected_core_tools = {
                    "list_allowed_projects",
                    "get_project_status",
                    "get_effective_project_policy",
                    "list_development_jobs",
                    "read_project_file_range",
                    "get_project_file_metadata",
                }
                for name in expected_core_tools:
                    self.assertIn(name, tool_map)
                    self.assertIsNotNone(tool_map[name].inputSchema)
                    self.assertIn("type", tool_map[name].inputSchema)

        asyncio.run(run_handshake())

    def test_client_read_only_tool_suite(self) -> None:
        async def run_read_only() -> None:
            async with Client(self.server) as client:
                # 1. list_allowed_projects
                res_projects = await client.call_tool("list_allowed_projects")
                self.assertFalse(res_projects.is_error)
                self.assertIn("projects", res_projects.data)
                project_ids = [p["project_id"] for p in res_projects.data["projects"]]
                self.assertIn("lifecycle-project", project_ids)

                # 2. get_project_status
                res_status = await client.call_tool(
                    "get_project_status",
                    {"project_id": "lifecycle-project"},
                )
                self.assertFalse(res_status.is_error)
                self.assertEqual(res_status.data["project_id"], "lifecycle-project")
                self.assertIn("repository", res_status.data)
                self.assertTrue(res_status.data["repository"]["clean"])
                self.assertEqual(res_status.data["repository"]["head"], self.initial_head)

                # 3. get_effective_project_policy
                res_policy = await client.call_tool(
                    "get_effective_project_policy",
                    {"project_id": "lifecycle-project"},
                )
                self.assertFalse(res_policy.is_error)
                self.assertIn("capabilities", res_policy.data)
                self.assertIn(
                    "repository_read",
                    res_policy.data["capabilities"],
                )

                # 4. list_project_markdown
                res_md_list = await client.call_tool(
                    "list_project_markdown",
                    {"project_id": "lifecycle-project"},
                )
                self.assertFalse(res_md_list.is_error)
                paths = [f["path"] for f in res_md_list.data["files"]]
                self.assertIn("README.md", paths)
                self.assertIn("docs/guide.md", paths)

                # 5. read_project_markdown
                res_read = await client.call_tool(
                    "read_project_markdown",
                    {"project_id": "lifecycle-project", "path": "docs/guide.md"},
                )
                self.assertFalse(res_read.is_error)
                self.assertIn("# User Guide", res_read.data["content"])

                # 6. read_project_file_range
                res_range = await client.call_tool(
                    "read_project_file_range",
                    {
                        "project_id": "lifecycle-project",
                        "path": "docs/guide.md",
                        "start_line": 1,
                        "end_line": 2,
                    },
                )
                self.assertFalse(res_range.is_error)
                self.assertEqual(res_range.data["start_line"], 1)
                self.assertEqual(res_range.data["end_line"], 2)
                self.assertIn("# User Guide", res_range.data["content"])

                # 7. get_project_file_metadata
                res_meta = await client.call_tool(
                    "get_project_file_metadata",
                    {"project_id": "lifecycle-project", "path": "docs/guide.md"},
                )
                self.assertFalse(res_meta.is_error)
                self.assertEqual(res_meta.data["line_count"], 3)
                self.assertTrue(isinstance(res_meta.data["sha256"], str))

                # 8. list_development_jobs
                res_jobs = await client.call_tool(
                    "list_development_jobs",
                    {"project_id": "lifecycle-project", "scope": "all", "limit": 10},
                )
                self.assertFalse(res_jobs.is_error)
                self.assertEqual(res_jobs.data["jobs"], [])

        asyncio.run(run_read_only())

    def test_client_safe_negative_calls_and_git_immutability(self) -> None:
        async def run_negative() -> None:
            async with Client(self.server) as client:
                # 1. Unknown project rejection
                with self.assertRaises(ToolError) as ctx_unknown:
                    await client.call_tool(
                        "get_project_status",
                        {"project_id": "nonexistent-project"},
                    )
                self.assertIn("unknown or disabled project_id", str(ctx_unknown.exception))

                # 2. Disabled capability rejection (lifecycle-project lacks markdown_write)
                with self.assertRaises(ToolError) as ctx_cap:
                    await client.call_tool(
                        "write_project_markdown",
                        {
                            "project_id": "lifecycle-project",
                            "path": "docs/guide.md",
                            "content": "Malicious edit",
                            "write_mode": "create",
                        },
                    )
                self.assertIn("does not allow markdown_write", str(ctx_cap.exception))

                # 3. Path traversal rejection
                with self.assertRaises(ToolError) as ctx_trav:
                    await client.call_tool(
                        "read_project_markdown",
                        {"project_id": "lifecycle-project", "path": "../outside.md"},
                    )
                self.assertIn("normalized relative path", str(ctx_trav.exception))

                with self.assertRaises(ToolError) as ctx_trav_range:
                    await client.call_tool(
                        "read_project_file_range",
                        {
                            "project_id": "lifecycle-project",
                            "path": "../outside.md",
                            "start_line": 1,
                            "end_line": 5,
                        },
                    )
                self.assertIn("normalized relative path", str(ctx_trav_range.exception))

        asyncio.run(run_negative())

        # Assert canonical repository remains completely untouched
        git_status = (
            subprocess.check_output(
                ["git", "-C", str(self.repo_dir), "status", "--short"]
            )
            .decode("utf-8")
            .strip()
        )
        self.assertEqual(git_status, "")

        git_head = (
            subprocess.check_output(
                ["git", "-C", str(self.repo_dir), "rev-parse", "HEAD"]
            )
            .decode("utf-8")
            .strip()
        )
        self.assertEqual(git_head, self.initial_head)

    def test_client_disconnect_reconnect_and_concurrent_calls(self) -> None:
        async def run_lifecycle() -> None:
            # Session 1: connect and disconnect
            async with Client(self.server) as client_1:
                res_1 = await client_1.call_tool("list_allowed_projects")
                self.assertFalse(res_1.is_error)

            # Session 2: reconnect with a fresh Client session
            async with Client(self.server) as client_2:
                # Concurrent read-only calls
                results = await asyncio.gather(
                    client_2.call_tool("list_allowed_projects"),
                    client_2.call_tool(
                        "get_project_status",
                        {"project_id": "lifecycle-project"},
                    ),
                    client_2.call_tool(
                        "get_effective_project_policy",
                        {"project_id": "lifecycle-project"},
                    ),
                    client_2.call_tool(
                        "read_project_file_range",
                        {
                            "project_id": "lifecycle-project",
                            "path": "docs/guide.md",
                            "start_line": 1,
                            "end_line": 2,
                        },
                    ),
                )
                self.assertEqual(len(results), 4)
                for res in results:
                    self.assertFalse(res.is_error)

        asyncio.run(run_lifecycle())


if __name__ == "__main__":
    unittest.main()
