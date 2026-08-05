from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .claude import ClaudeExecutor
from .config import Settings
from .git import project_status
from .models import Effort, ProjectCapability
from .reader import RepositoryReader
from .registry import ProjectLockManager, ProjectRegistry
from .writer import RepositoryWriter, WriteMode


def create_server(
    settings: Settings | None = None,
    registry: ProjectRegistry | None = None,
    executor: ClaudeExecutor | None = None,
    reader: RepositoryReader | None = None,
    writer: RepositoryWriter | None = None,
    locks: ProjectLockManager | None = None,
) -> FastMCP:
    active_settings = settings or Settings.from_env()
    active_registry = registry or ProjectRegistry.load(
        active_settings.projects_config_path
    )
    active_locks = locks or ProjectLockManager()
    active_executor = executor or ClaudeExecutor(active_settings)
    active_reader = reader or RepositoryReader(active_settings)
    active_writer = writer or RepositoryWriter(active_settings)

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

    read_only_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(
        name="list_project_markdown",
        description=(
            "List Markdown files beneath an allowed project directory without "
            "reading their contents. Use this to discover exact repository paths."
        ),
        annotations=read_only_annotations,
    )
    async def list_project_markdown(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str,
            Field(
                description="Relative directory to list; use '.' for the project root."
            ),
        ] = ".",
        max_depth: Annotated[
            int,
            Field(description="Maximum directory depth below path, from 0 through 20.", ge=0, le=20),
        ] = 3,
        max_results: Annotated[
            int,
            Field(description="Maximum number of Markdown paths to return.", ge=1),
        ] = 100,
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.list_markdown(project, path, max_depth, max_results)

    @mcp.tool(
        name="search_project_markdown",
        description=(
            "Search Markdown files in an allowed project for a literal, "
            "case-insensitive text query. Returns bounded path, line, column, "
            "and excerpt matches; it never executes a shell command."
        ),
        annotations=read_only_annotations,
    )
    async def search_project_markdown(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        query: Annotated[
            str, Field(description="Literal text to find in Markdown files.", min_length=1, max_length=500)
        ],
        path: Annotated[
            str, Field(description="Relative directory or Markdown file to search.")
        ] = ".",
        max_results: Annotated[
            int, Field(description="Maximum number of matches to return.", ge=1)
        ] = 30,
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.search(project, query, path, max_results)

    @mcp.tool(
        name="read_project_markdown",
        description=(
            "Read one complete UTF-8 Markdown file from an exact allowed project "
            "path, with SHA-256, size, line count, timestamp, and Git status."
        ),
        annotations=read_only_annotations,
    )
    async def read_project_markdown(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str, Field(description="Exact relative path to a Markdown (.md) file.")
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.read_markdown(project, path)

    @mcp.tool(
        name="read_project_file_range",
        description=(
            "Read a bounded line range from one Markdown file. Supply the SHA-256 "
            "from a prior read when consistency across multiple reads matters."
        ),
        annotations=read_only_annotations,
    )
    async def read_project_file_range(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str, Field(description="Exact relative path to a Markdown (.md) file.")
        ],
        start_line: Annotated[int, Field(description="First 1-based line.", ge=1)],
        end_line: Annotated[int, Field(description="Last 1-based line, inclusive.", ge=1)],
        expected_sha256: Annotated[
            str | None,
            Field(description="Optional SHA-256 requiring the file to be unchanged."),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.read_range(
            project, path, start_line, end_line, expected_sha256
        )

    @mcp.tool(
        name="get_markdown_outline",
        description=(
            "Return ATX and setext Markdown headings with levels and line numbers, "
            "excluding headings inside fenced code blocks."
        ),
        annotations=read_only_annotations,
    )
    async def get_markdown_outline(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str, Field(description="Exact relative path to a Markdown (.md) file.")
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.outline(project, path)

    @mcp.tool(
        name="get_project_file_metadata",
        description=(
            "Get SHA-256, byte size, line count, modified timestamp, and Git status "
            "for one Markdown file without returning its content."
        ),
        annotations=read_only_annotations,
    )
    async def get_project_file_metadata(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str, Field(description="Exact relative path to a Markdown (.md) file.")
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await active_reader.metadata(project, path)

    @mcp.tool(
        name="write_project_markdown",
        description=(
            "Create or update one UTF-8 Markdown file beneath an approved write "
            "root. Create never overwrites. Update requires the current SHA-256 "
            "and fails if the file changed. Parent directories must already exist."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def write_project_markdown(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        path: Annotated[
            str,
            Field(
                description=(
                    "Normalized project-relative .md path beneath an approved "
                    "Markdown write root."
                )
            ),
        ],
        content: Annotated[
            str,
            Field(
                description=(
                    "Complete UTF-8 Markdown content. Line endings and final newline "
                    "are preserved exactly."
                )
            ),
        ],
        write_mode: Annotated[
            WriteMode,
            Field(description="Use create for a missing file or update for an existing file."),
        ] = "create",
        expected_sha256: Annotated[
            str | None,
            Field(
                description=(
                    "Required for update and forbidden for create: the current "
                    "64-character hexadecimal SHA-256."
                ),
                pattern=r"^[0-9A-Fa-f]{64}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.MARKDOWN_WRITE)
        lock = await active_locks.for_project(project.id)
        async with lock:
            return await active_writer.write(
                project, path, content, write_mode, expected_sha256
            )

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
        lock = await active_locks.for_project(project.id)
        async with lock:
            return await active_executor.execute(project, text, effort)

    return mcp


def run() -> None:
    settings = Settings.from_env()
    server = create_server(settings=settings)
    server.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )
