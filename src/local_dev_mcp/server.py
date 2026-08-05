from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .claude import ClaudeExecutor
from .config import Settings
from .git import project_status
from .models import Effort, ProjectCapability
from .registry import ProjectLockManager, ProjectRegistry


def create_server(
    settings: Settings | None = None,
    registry: ProjectRegistry | None = None,
) -> FastMCP:
    active_settings = settings or Settings.from_env()
    active_registry = registry or ProjectRegistry.load(
        active_settings.projects_config_path
    )
    locks = ProjectLockManager()
    executor = ClaudeExecutor(active_settings)

    mcp = FastMCP(
        name="Local Development Bridge",
        instructions=(
            "Before any project action, use the exact project_id supplied by "
            "the user or call list_allowed_projects; never guess a project. "
            "The server authorizes every project and capability. Use high for "
            "routine work and ultracode only for complex architecture, difficult "
            "reasoning, migrations, or substantial cross-module implementation. "
            "An explicit user effort choice wins. Development calls may modify files."
        ),
    )

    @mcp.tool(
        name="list_allowed_projects",
        description=(
            "List the project IDs this bridge is authorized to access. Call this "
            "when a project name is missing or ambiguous. Never invent a project ID."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def list_allowed_projects() -> dict[str, object]:
        return {"projects": active_registry.list_public()}

    @mcp.tool(
        name="get_project_status",
        description=(
            "Get Git branch, HEAD, cleanliness, and diff summary for one exact "
            "allowed project before planning or delegating work."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def get_project_status(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
    ) -> dict[str, object]:
        project = active_registry.resolve(
            project_id, ProjectCapability.REPOSITORY_READ
        )
        return {
            "project_id": project.id,
            "project_name": project.display_name,
            "repository": await project_status(project.root),
        }

    @mcp.tool(
        name="send_to_local_dev",
        description=(
            "Delegate an approved work order to Claude Code in one exact allowed "
            "project. Use high for routine analysis and implementation. Use "
            "ultracode only for complex architecture, difficult reasoning, "
            "cross-module implementation, migrations, or explicit user requests. "
            "Claude may modify files; Bash, web, and secondary MCP tools are disabled."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def send_to_local_dev(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        text: Annotated[
            str,
            Field(
                description=(
                    "Complete approved architecture specification, implementation "
                    "work order, or engineering instruction."
                ),
                min_length=1,
            ),
        ],
        effort: Annotated[
            Effort,
            Field(
                description=(
                    "Use high for routine work; use ultracode only for complex "
                    "reasoning or substantial implementation."
                )
            ),
        ] = "high",
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        lock = await locks.for_project(project.id)
        async with lock:
            return await executor.execute(project, text, effort)

    return mcp


def run() -> None:
    settings = Settings.from_env()
    server = create_server(settings=settings)
    server.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )

