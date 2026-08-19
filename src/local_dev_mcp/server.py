from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .claude import ClaudeExecutor
from .config import Settings
from .evidence import EvidenceBundleError, EvidenceBundleManager
from .git import project_status
from .introspection import CanonicalizationResolver, effective_policy_view
from .jobs import DevelopmentJobManager
from .models import Effort, ProjectCapability
from .pr import PullRequestError, PullRequestManager
from .reader import RepositoryReader
from .registry import ProjectLockManager, ProjectRegistry
from .review import ReviewError, ReviewManager
from .writer import RepositoryWriter, WriteMode
from .verification import PublicationManager, VerificationCommitManager
from .sync import SyncError, SyncManager


def create_server(
    settings: Settings | None = None,
    registry: ProjectRegistry | None = None,
    executor: ClaudeExecutor | None = None,
    reader: RepositoryReader | None = None,
    writer: RepositoryWriter | None = None,
    locks: ProjectLockManager | None = None,
    jobs: DevelopmentJobManager | None = None,
    verification: VerificationCommitManager | None = None,
    review: ReviewManager | None = None,
    publication: PublicationManager | None = None,
    sync: SyncManager | None = None,
    evidence: EvidenceBundleManager | None = None,
    pr: PullRequestManager | None = None,
    canonicalization: CanonicalizationResolver | None = None,
) -> FastMCP:
    active_settings = settings or Settings.from_env()
    active_registry = registry or ProjectRegistry.load(
        active_settings.projects_config_path
    )
    active_locks = locks or ProjectLockManager()
    active_executor = executor or ClaudeExecutor(active_settings)
    active_reader = reader or RepositoryReader(active_settings)
    active_writer = writer or RepositoryWriter(active_settings)
    active_jobs = jobs or DevelopmentJobManager(
        active_settings,
        active_locks,
        active_executor,
        projects=active_registry.projects(),
    )
    active_verification = verification or VerificationCommitManager(
        active_settings,
        active_locks,
        active_jobs,
        projects=active_registry.projects(),
    )
    active_review = review or ReviewManager(
        active_settings,
        active_locks,
        active_jobs,
        active_executor,
        projects=active_registry.projects(),
    )
    active_publication = publication or PublicationManager(
        active_settings,
        active_locks,
        active_jobs,
        projects=active_registry.projects(),
    )
    active_sync = sync or SyncManager(
        active_settings,
        active_locks,
        active_jobs,
        projects=active_registry.projects(),
    )
    active_evidence = evidence or EvidenceBundleManager(
        active_settings,
        active_locks,
        active_jobs,
        projects=active_registry.projects(),
    )
    active_pr = pr or PullRequestManager(
        active_settings,
        active_locks,
        active_jobs,
        projects=active_registry.projects(),
    )
    active_canonicalization = canonicalization or CanonicalizationResolver(
        active_settings,
        active_locks,
        active_jobs,
        active_pr,
        active_sync,
        projects=active_registry.projects(),
    )

    mcp = FastMCP(
        name="Local Development Bridge",
        instructions=(
            "Before any project action, use the exact project_id supplied by "
            "the user or call list_allowed_projects; never guess a project. "
            "The server authorizes every project and capability. Use high for "
            "routine work and ultracode only for complex architecture, difficult "
            "reasoning, migrations, or substantial cross-module implementation. "
            "An explicit user effort choice wins. Development jobs may modify only "
            "their bridge-created detached worktrees. Verification may run only "
            "registry-configured commands. Never commit until the user separately "
            "approves the exact verification evidence, paths, and message. A "
            "detached commit may be published only to a brand-new remote branch "
            "that is not the repository default, main, or master; never force-push, "
            "merge, tag, move another ref, or update a trunk. The canonical "
            "checkout may be synchronized only to the verified current tip of "
            "origin's default branch, by fast-forward only, when clean and on the "
            "default branch; sync never pushes, forces, merges, tags, or moves a "
            "remote ref."
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
            "Start an asynchronous Claude Code job in a detached Git worktree "
            "for one exact allowed project. Returns an opaque job ID immediately. "
            "Use high for routine analysis and implementation. Use "
            "ultracode only for complex architecture, difficult reasoning, "
            "cross-module implementation, migrations, or explicit user requests. "
            "The configured checkout is never edited directly; Bash, web, and "
            "secondary MCP tools are disabled."
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
        requested_base: Annotated[
            str | None,
            Field(
                description=(
                    "Optional 40-hex commit to pin the job worktree to. When "
                    "set, the bridge fetches origin and requires this to be the "
                    "current tip of origin's default branch, branching the job "
                    "off that fetched remote tip instead of the local HEAD. "
                    "Omit for offline/local-only projects."
                ),
                pattern=r"^[0-9a-fA-F]{40}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        return await active_jobs.start(project, text, effort, requested_base)

    @mcp.tool(
        name="get_development_job",
        description=(
            "Get the current status and bounded result of one bridge-created "
            "development job. The result never exposes its worktree path or raw "
            "Claude session ID."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def get_development_job(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        return await active_jobs.get(project, job_id)

    @mcp.tool(
        name="resume_development_job",
        description=(
            "Continue a terminal development job in its retained worktree using "
            "only the Claude session ID previously captured by this bridge. "
            "Callers provide a job ID and never provide a raw session ID."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def resume_development_job(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        text: Annotated[
            str,
            Field(
                description="Complete approved continuation instruction.",
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
        return await active_jobs.resume(project, job_id, text, effort)

    @mcp.tool(
        name="cancel_development_job",
        description=(
            "Cancel one queued or running bridge-created development job and "
            "terminate its process group. Candidate files and the isolated "
            "worktree are retained for inspection."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def cancel_development_job(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        return await active_jobs.cancel(project, job_id)

    @mcp.tool(
        name="verify_development_job",
        description=(
            "Run only the fixed verification commands configured for a completed "
            "retained development job. Success binds structured command evidence "
            "to the exact base HEAD, changed paths, and candidate fingerprint. "
            "The caller cannot supply a command, argument, environment, or path."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def verify_development_job(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        expected_head: Annotated[
            str,
            Field(
                description="Exact lowercase base HEAD reported for the retained job.",
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.VERIFICATION_COMMIT
        )
        return await active_verification.verify(project, job_id, expected_head)

    @mcp.tool(
        name="list_candidate_changes",
        description=(
            "List the changed paths of a completed development job's retained "
            "candidate with bounded metadata (size, SHA-256, executable flag, or "
            "deleted). Reads only the candidate's changed-path set, never the "
            "worktree at large. Does not modify the candidate or run commands."
        ),
        annotations=read_only_annotations,
    )
    async def list_candidate_changes(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await active_review.list_candidate_changes(project, job_id)

    @mcp.tool(
        name="read_candidate_file",
        description=(
            "Read one file from a completed job's retained candidate, scoped "
            "strictly to the candidate's changed-path set. Supports an optional "
            "bounded line range and an optional SHA-256 consistency check. "
            "Deletions are reported without content. The worktree path and raw "
            "session ID are never exposed."
        ),
        annotations=read_only_annotations,
    )
    async def read_candidate_file(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        path: Annotated[
            str,
            Field(
                description=(
                    "One changed path from list_candidate_changes; must be a "
                    "normalized relative path without '.', '..', or empty segments."
                ),
                min_length=1,
            ),
        ],
        start_line: Annotated[
            int | None,
            Field(description="Optional first 1-based line; requires end_line.", ge=1),
        ] = None,
        end_line: Annotated[
            int | None,
            Field(description="Optional last 1-based line, inclusive; requires start_line.", ge=1),
        ] = None,
        expected_sha256: Annotated[
            str | None,
            Field(
                description="Optional 64-character SHA-256 requiring the file to be unchanged.",
                pattern=r"^[0-9A-Fa-f]{64}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await active_review.read_candidate_file(
            project, job_id, path, start_line, end_line, expected_sha256
        )

    @mcp.tool(
        name="record_candidate_review",
        description=(
            "Record a reviewer's structured verdict for a completed job's "
            "candidate, bound to the exact current candidate state. The verdict "
            "is a non-empty JSON object. The resulting review_id is single-use "
            "and may be required by commit_verified_development_job for projects "
            "that enable requires_review. Does not modify the candidate."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def record_candidate_review(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        verdict: Annotated[
            dict[str, Any],
            Field(
                description=(
                    "Non-empty structured verdict object, e.g. "
                    "{\"summary\": ..., \"issues\": [...], \"recommendation\": ...}."
                ),
            ),
        ],
        reviewer_id: Annotated[
            str | None,
            Field(
                description="Optional short reviewer label (at most 200 printable characters).",
            ),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await active_review.record_candidate_review(
            project, job_id, verdict, reviewer_id
        )

    @mcp.tool(
        name="run_independent_review",
        description=(
            "Spawn a fresh, read-only Claude reviewer over a completed job's "
            "candidate. The reviewer is a distinct session (never resumed from "
            "the drafting session) with only Read, Glob, and Grep, so it can "
            "inspect but not modify. Requires the project to allow a "
            "bridge-spawned reviewer. Returns a single-use review_id bound to the "
            "exact candidate state."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def run_independent_review(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        effort: Annotated[
            Effort,
            Field(
                description=(
                    "Use high for routine review; use ultracode only for complex "
                    "review requiring deep reasoning."
                )
            ),
        ] = "high",
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await active_review.run_independent_review(project, job_id, effort)

    @mcp.tool(
        name="get_candidate_review",
        description=(
            "Retrieve a previously recorded candidate review verdict by its "
            "single-use review_id, without re-running a reviewer. Read-only."
        ),
        annotations=read_only_annotations,
    )
    async def get_candidate_review(
        project_id: Annotated[
            str, Field(description="Exact project ID returned by list_allowed_projects.")
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        review_id: Annotated[
            str,
            Field(
                description="Single-use review ID from record_candidate_review or run_independent_review.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await active_review.get_candidate_review(project, job_id, review_id)

    @mcp.tool(
        name="commit_verified_development_job",
        description=(
            "After separate human approval, create one detached commit from the "
            "exact unchanged state that passed verify_development_job. Requires "
            "the verification ID, base HEAD, fingerprint, and complete ordered "
            "path list. It never updates a branch, pushes, merges, tags, promotes, "
            "or restarts production. For projects that enable requires_review, a "
            "matching review_id is also required and is consumed once; the gate "
            "enforces that a review record bound to this exact candidate exists, "
            "not that the review was independent — independence is the operator's "
            "choice of channel (run_independent_review, attested as reviewer_id "
            "'bridge-reviewer', versus a caller-supplied record_candidate_review)."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def commit_verified_development_job(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        verification_id: Annotated[
            str,
            Field(
                description="Single-use ID returned by a successful verification.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        expected_head: Annotated[
            str,
            Field(
                description="Exact lowercase base HEAD from verification evidence.",
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
        expected_diff_fingerprint: Annotated[
            str,
            Field(
                description="Exact lowercase SHA-256 from verification evidence.",
                pattern=r"^[0-9a-f]{64}$",
            ),
        ],
        paths: Annotated[
            list[str],
            Field(
                description=(
                    "Complete ordered changed-path list from verification evidence; "
                    "the server requires exact equality and stages only these paths."
                ),
                min_length=1,
                max_length=500,
            ),
        ],
        message: Annotated[
            str,
            Field(description="Human-approved commit message.", min_length=1),
        ],
        review_id: Annotated[
            str | None,
            Field(
                description=(
                    "Optional single-use review ID. Required for projects with "
                    "requires_review; binds the commit to a review record of the "
                    "exact same candidate state. The gate checks the record matches "
                    "and is single-use, not that the review was independent — a "
                    "record whose reviewer_id is 'bridge-reviewer' was attested by "
                    "run_independent_review; any other reviewer_id was caller-supplied."
                ),
                pattern=r"^[0-9a-f]{32}$",
            ),
        ] = None,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.VERIFICATION_COMMIT
        )
        if project.requires_review:
            if review_id is None:
                raise ReviewError(
                    "review_id is required: this project requires a candidate "
                    "review before a verified commit"
                )
            await active_review.validate_for_commit(
                project,
                job_id,
                review_id,
                expected_head,
                expected_diff_fingerprint,
                paths,
            )
        committed = await active_verification.commit(
            project,
            job_id,
            verification_id,
            expected_head,
            expected_diff_fingerprint,
            paths,
            message,
        )
        if project.requires_review and review_id is not None:
            try:
                await active_review.mark_used(review_id)
            except Exception:
                # The commit already succeeded and verification's single-use
                # verification_id prevents a second commit, so a review-
                # bookkeeping failure must not discard the commit_sha the caller
                # needs. mark_used is atomic, so the review stays reusable in
                # memory and disk; surface the issue as a warning instead.
                committed = {
                    **committed,
                    "review_mark_warning": (
                        "commit succeeded but the review record could not be "
                        "marked used; the single-use verification_id still "
                        "prevents a second commit"
                    ),
                }
        return committed

    @mcp.tool(
        name="push_detached_development_commit",
        description=(
            "After separate human approval, publish the exact detached commit "
            "created by commit_verified_development_job to one new remote branch "
            "on origin. Requires the job ID, the exact commit SHA, the new "
            "branch name, and the verified base HEAD and diff fingerprint. Rejects "
            "the repository default branch, main, and master; requires the remote "
            "branch not to exist; never force-pushes, merges, tags, or moves "
            "another ref. Confirms the commit belongs to the retained job and "
            "realizes the verified candidate fingerprint. One branch per job; "
            "retrying the same commit and branch is idempotent. Returns the "
            "remote branch name, commit SHA, and repository URL."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def push_detached_development_commit(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque completed job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        commit_sha: Annotated[
            str,
            Field(
                description="Exact lowercase detached commit SHA from commit_verified_development_job.",
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
        branch_name: Annotated[
            str,
            Field(
                description=(
                    "Short name for a new remote branch; must not be a full ref, "
                    "HEAD, the repository default, main, or master."
                ),
                min_length=1,
                max_length=200,
            ),
        ],
        expected_base_head: Annotated[
            str,
            Field(
                description="Exact lowercase base HEAD from verification evidence.",
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
        expected_diff_fingerprint: Annotated[
            str,
            Field(
                description="Exact lowercase SHA-256 from verification evidence.",
                pattern=r"^[0-9a-f]{64}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.PUSH_DEVELOPMENT_COMMIT
        )
        return await active_publication.publish(
            project,
            job_id,
            commit_sha,
            branch_name,
            expected_base_head,
            expected_diff_fingerprint,
        )

    @mcp.tool(
        name="sync_canonical_checkout",
        description=(
            "After a pull request merges on the remote default branch, "
            "fast-forward one exact allowed project's guarded canonical "
            "checkout to the verified current tip of origin's default branch. "
            "Requires the exact current HEAD (the stale commit, from "
            "get_project_status) and the target commit. The canonical checkout "
            "must be clean and on the repository default branch; the target "
            "must equal origin/<default> after a fetch. Updates the local "
            "default branch by fast-forward only; never pushes, forces, merges, "
            "tags, deletes, or moves a remote ref. Refuses while a non-terminal "
            "development job exists; warns about completed-uncommitted jobs "
            "that will lose their verify/commit path after the base moves."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def sync_canonical_checkout(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        expected_current_head: Annotated[
            str,
            Field(
                description=(
                    "Exact lowercase 40-hex SHA the canonical checkout is "
                    "currently at; obtain it from get_project_status."
                ),
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
        target_commit: Annotated[
            str,
            Field(
                description=(
                    "Exact lowercase 40-hex SHA to synchronize to; must equal "
                    "the current tip of origin's default branch."
                ),
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.SYNC_CANONICAL_CHECKOUT
        )
        try:
            return await active_sync.sync(project, expected_current_head, target_commit)
        except SyncError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool(
        name="prepare_canonical_for_work",
        description=(
            "Pre-work canonical-checkout update check. Fetches origin and reports "
            "whether the canonical checkout is up to date with, behind, ahead of, "
            "or diverged from origin's default branch, plus dirty/no-origin/"
            "fetch-failed states — without merging. Call with pull=false (default) "
            "to see whether updates are available; when the verdict is "
            "updates_available, call again with pull=true to fast-forward the "
            "canonical checkout to the verified remote tip (auto-discovered; no "
            "SHA arguments required). The pull reuses sync_canonical_checkout's "
            "safety: clean checkout, on the default branch, fast-forward only; "
            "never pushes, forces, merges, tags, deletes, or moves a remote ref. "
            "Refuses while a non-terminal development job exists. A diverged state "
            "cannot be fast-forwarded and still requires a human steward. Opt-in "
            "via the sync_canonical_checkout project capability."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def prepare_canonical_for_work(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        pull: Annotated[
            bool,
            Field(
                description=(
                    "false (default): fetch and report the update state without "
                    "merging. true: fast-forward the canonical checkout to the "
                    "verified remote tip when updates are available."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.SYNC_CANONICAL_CHECKOUT
        )
        if not pull:
            return await active_sync.check_for_updates(project)
        try:
            return await active_sync.sync(
                project,
                expected_current_head=None,
                target_commit=None,
            )
        except SyncError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool(
        name="export_evidence_bundle",
        description=(
            "Assemble a single redacted, content-addressed evidence bundle for "
            "one development job: candidate fingerprint + changed paths + diff "
            "summary, verification records + bindings, review records + verdict "
            "hashes, checkpoint summary, and a bundle-wide SHA-256. Requires the "
            "expected candidate fingerprint to bind the bundle to a specific "
            "candidate. Never exposes raw worktree paths, secrets, or "
            "credentials. Read-only; opt-in via the export_evidence_bundle "
            "project capability."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def export_evidence_bundle(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        expected_candidate_fingerprint: Annotated[
            str,
            Field(
                description=(
                    "Exact lowercase 64-hex SHA-256 candidate fingerprint the "
                    "bundle is bound to."
                ),
                pattern=r"^[0-9a-f]{64}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.EXPORT_EVIDENCE_BUNDLE
        )
        try:
            return await active_evidence.export_bundle(
                project,
                job_id,
                expected_candidate_fingerprint,
            )
        except EvidenceBundleError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.tool(
        name="open_pull_request",
        description=(
            "Open or update a draft pull request for a development job's "
            "published remote branch. Discovers an existing PR for the branch "
            "and updates it, or opens a new draft PR. Confirms the remote "
            "branch and PR state. Never merges. Requires the job to have been "
            "published (push_development_commit). Opt-in via the "
            "open_pull_request project capability."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def open_pull_request(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        job_id: Annotated[
            str,
            Field(
                description="Opaque job ID returned by send_to_local_dev.",
                pattern=r"^[0-9a-f]{32}$",
            ),
        ],
        base_head: Annotated[
            str,
            Field(
                description="Exact lowercase 40-hex SHA the job branched from.",
                pattern=r"^[0-9a-f]{40}$",
            ),
        ],
        fingerprint: Annotated[
            str,
            Field(
                description="Exact lowercase 64-hex SHA-256 candidate fingerprint.",
                pattern=r"^[0-9a-f]{64}$",
            ),
        ],
        title: Annotated[
            str,
            Field(description="Pull request title.", min_length=1, max_length=200),
        ],
        draft: Annotated[
            bool,
            Field(description="Open as a draft PR (default true)."),
        ] = True,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.OPEN_PULL_REQUEST
        )
        try:
            return await active_pr.open_pull_request(
                project,
                job_id,
                base_head,
                fingerprint,
                title,
                draft=draft,
            )
        except PullRequestError as exc:
            raise ValueError(str(exc)) from exc

    # M-AL-01 read-only introspection tools (dormant by default; opt-in via the
    # job_introspection / policy_introspection / canonical_history_read project
    # capabilities). They only observe governed state and never mutate: they read
    # the in-memory + durable job store, the already-validated effective policy,
    # and origin history via read-only git/gh (no fetch, no push, no merge).

    @mcp.tool(
        name="list_development_jobs",
        description=(
            "Enumerate this project's durable development jobs without a job ID. "
            "Returns a bounded, sanitized list (no worktree path or Claude session "
            "ID) with per-job governance state (verification/review/publication "
            "presence). Use scope=active for in-flight jobs, terminal for finished "
            "jobs, or all (default). Opt-in via the job_introspection capability."
        ),
        annotations=read_only_annotations,
    )
    async def list_development_jobs(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        scope: Annotated[
            Literal["all", "active", "terminal"],
            Field(
                description=(
                    "Job lifecycle filter: all (default), active (in-flight), or "
                    "terminal (completed/failed/cancelled)."
                ),
            ),
        ] = "all",
        limit: Annotated[
            int,
            Field(
                description="Maximum number of jobs to return (default 50, hard cap 200).",
                ge=0,
            ),
        ] = 50,
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.JOB_INTROSPECTION
        )
        return await active_jobs.list_jobs(project, scope, limit)

    @mcp.tool(
        name="get_effective_project_policy",
        description=(
            "Expose the validated effective policy for one allowed project as a "
            "deterministic, redacted view with a policy_fingerprint. The absolute "
            "root is never returned. Use this to reason about a project's "
            "authorization surface without re-parsing its JSON. Opt-in via the "
            "policy_introspection capability."
        ),
        annotations=read_only_annotations,
    )
    async def get_effective_project_policy(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.POLICY_INTROSPECTION
        )
        redaction_paths = (
            project.root,
            active_settings.job_worktree_root,
            active_settings.projects_config_path,
            *(policy.root for policy in active_registry.projects()),
        )
        return effective_policy_view(project, redaction_paths)

    @mcp.tool(
        name="resolve_candidate_canonicalization",
        description=(
            "Reconcile a detached development commit against the project's "
            "canonical history. Given a 40-hex commit SHA, returns a three-state "
            "verdict: CANONICAL (absorbed into origin's default branch by direct "
            "ancestry or squash/rebase content-equivalence), NOT_CANONICAL "
            "(provenance-linked and published but not yet absorbed), or "
            "INDETERMINATE (provenance missing, content mismatch, or canonical tip "
            "unreadable). Read-only and origin-only: never fetches, pushes, merges, "
            "tags, or moves a ref. Opt-in via the canonical_history_read capability."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def resolve_candidate_canonicalization(
        project_id: Annotated[
            str,
            Field(description="Exact project ID returned by list_allowed_projects."),
        ],
        commit_sha: Annotated[
            str,
            Field(
                description="Exact 40-hex SHA of the candidate detached commit.",
                pattern=r"^[0-9a-fA-F]{40}$",
            ),
        ],
    ) -> dict[str, Any]:
        project = active_registry.resolve(
            project_id, ProjectCapability.CANONICAL_HISTORY_READ
        )
        return await active_canonicalization.resolve(project, commit_sha)

    return mcp


def run() -> None:
    settings = Settings.from_env()
    server = create_server(settings=settings)
    server.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
    )
