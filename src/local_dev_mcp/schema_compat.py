"""Public MCP tool catalog, schema compatibility, and drift detection.

Defines the authoritative public tool catalog covering all 39 registered MCP
tools in local_dev_mcp.server, their lifecycle stages, capabilities,
ToolAnnotations, error families, and validation helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterable

from .models import ProjectCapability


class ToolLifecycleStage(StrEnum):
    """Lifecycle stage of a tool surface in the MCP bridge."""

    CORE_V1 = "core_v1"
    SECONDARY_V1 = "secondary_v1"
    QUARANTINED_PRE_V1 = "quarantined_pre_v1"


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Metadata specification for a public MCP tool."""

    name: str
    owner_service: str
    stage: ToolLifecycleStage
    capability: ProjectCapability | None
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool
    error_family: str
    notes: str = ""


PUBLIC_TOOL_CATALOG: dict[str, ToolCatalogEntry] = {
    "cancel_development_job": ToolCatalogEntry(
        name="cancel_development_job",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CLAUDE_EDIT,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=False,
        error_family="job_lifecycle_error",
        notes="Cancel an active development job and release isolated resources.",
    ),
    "commit_verified_development_job": ToolCatalogEntry(
        name="commit_verified_development_job",
        owner_service="VerificationCommitManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.VERIFICATION_COMMIT,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
        error_family="verification_failed",
        notes="Commit verified changes to detached worktree with review authorization.",
    ),
    "discard_candidate": ToolCatalogEntry(
        name="discard_candidate",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.DISCARD_CANDIDATE,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=False,
        error_family="job_lifecycle_error",
        notes="Discard a candidate worktree and mark job discarded.",
    ),
    "export_evidence_bundle": ToolCatalogEntry(
        name="export_evidence_bundle",
        owner_service="EvidenceBundleManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.EXPORT_EVIDENCE_BUNDLE,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Export cryptographically verified audit bundle for completed work.",
    ),
    "extend_budget": ToolCatalogEntry(
        name="extend_budget",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.EXTEND_BUDGET,
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="validation_failed",
        notes="Extend turn or cost budget for an in-flight development job.",
    ),
    "get_candidate_review": ToolCatalogEntry(
        name="get_candidate_review",
        owner_service="ReviewManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANDIDATE_REVIEW,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Fetch review record for a candidate worktree.",
    ),
    "get_development_job": ToolCatalogEntry(
        name="get_development_job",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CLAUDE_EDIT,
        read_only=True,
        destructive=False,
        idempotent=False,
        open_world=False,
        error_family="not_found",
        notes="Inspect status, progress, and logs of a development job.",
    ),
    "get_effective_project_policy": ToolCatalogEntry(
        name="get_effective_project_policy",
        owner_service="ProjectRegistry",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.POLICY_INTROSPECTION,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="policy_denied",
        notes="Introspect effective governance policy and capabilities for a project.",
    ),
    "get_markdown_outline": ToolCatalogEntry(
        name="get_markdown_outline",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Secondary integration: extract heading outline from markdown document.",
    ),
    "get_project_file_metadata": ToolCatalogEntry(
        name="get_project_file_metadata",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Inspect file metadata, size, line count, and SHA-256 digest.",
    ),
    "get_project_status": ToolCatalogEntry(
        name="get_project_status",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=False,
        open_world=False,
        error_family="policy_denied",
        notes="Inspect canonical branch HEAD, clean status, and active job count.",
    ),
    "list_allowed_projects": ToolCatalogEntry(
        name="list_allowed_projects",
        owner_service="ProjectRegistry",
        stage=ToolLifecycleStage.CORE_V1,
        capability=None,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="runtime_error",
        notes="Public entrypoint: list all allowlisted projects with sanitized metadata.",
    ),
    "list_candidate_changes": ToolCatalogEntry(
        name="list_candidate_changes",
        owner_service="ReviewManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANDIDATE_REVIEW,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="List modified and added files in candidate worktree.",
    ),
    "list_development_jobs": ToolCatalogEntry(
        name="list_development_jobs",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.JOB_INTROSPECTION,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="policy_denied",
        notes="Introspect all development jobs matching scope filters.",
    ),
    "list_project_markdown": ToolCatalogEntry(
        name="list_project_markdown",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="policy_denied",
        notes="Secondary integration: list markdown files within allowed roots.",
    ),
    "narrow_scope": ToolCatalogEntry(
        name="narrow_scope",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.NARROW_SCOPE,
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="validation_failed",
        notes="Narrow allowed edit paths for an active development job.",
    ),
    "open_pull_request": ToolCatalogEntry(
        name="open_pull_request",
        owner_service="PullRequestManager",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.OPEN_PULL_REQUEST,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        error_family="publication_failed",
        notes="Secondary integration: open pull request on hosting remote.",
    ),
    "pause_development_job": ToolCatalogEntry(
        name="pause_development_job",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.PAUSE_DEVELOPMENT_JOB,
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="conflict_or_stale",
        notes="Pause an active development job for later resumption.",
    ),
    "preflight_working_tree_capture": ToolCatalogEntry(
        name="preflight_working_tree_capture",
        owner_service="WorkingTreeCaptureManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="capture_failed",
        notes="Read-only preflight of dirty working tree diff before capture.",
    ),
    "prepare_canonical_for_work": ToolCatalogEntry(
        name="prepare_canonical_for_work",
        owner_service="SyncManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.SYNC_CANONICAL_CHECKOUT,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        error_family="conflict_or_stale",
        notes="Fetch remote upstream and prepare canonical checkout for work.",
    ),
    "push_detached_development_commit": ToolCatalogEntry(
        name="push_detached_development_commit",
        owner_service="PublicationManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.PUSH_DEVELOPMENT_COMMIT,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        error_family="publication_failed",
        notes="Push verified detached commit to non-trunk remote branch.",
    ),
    "read_candidate_file": ToolCatalogEntry(
        name="read_candidate_file",
        owner_service="ReviewManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANDIDATE_REVIEW,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Read file contents from candidate isolated worktree.",
    ),
    "read_project_file_range": ToolCatalogEntry(
        name="read_project_file_range",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="validation_failed",
        notes="Read slice of file with 1-based start line and line count.",
    ),
    "read_project_markdown": ToolCatalogEntry(
        name="read_project_markdown",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="not_found",
        notes="Secondary integration: read whole markdown file content.",
    ),
    "record_candidate_review": ToolCatalogEntry(
        name="record_candidate_review",
        owner_service="ReviewManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANDIDATE_REVIEW,
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        error_family="review_failed",
        notes="Record human or automated review verdict on candidate worktree.",
    ),
    "replace_worker": ToolCatalogEntry(
        name="replace_worker",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REPLACE_WORKER,
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        error_family="validation_failed",
        notes="Swap executor backend/model for in-flight job.",
    ),
    "replay_verification": ToolCatalogEntry(
        name="replay_verification",
        owner_service="VerificationCommitManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REPLAY_VERIFICATION,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="verification_failed",
        notes="Replay verification command suite on previously verified state.",
    ),
    "resolve_candidate_canonicalization": ToolCatalogEntry(
        name="resolve_candidate_canonicalization",
        owner_service="CanonicalizationResolver",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANONICAL_HISTORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=True,
        error_family="conflict_or_stale",
        notes="Resolve if candidate commit has been merged into canonical upstream.",
    ),
    "resume_development_job": ToolCatalogEntry(
        name="resume_development_job",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CLAUDE_EDIT,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
        error_family="job_lifecycle_error",
        notes="Resume an active or paused development job with new instructions.",
    ),
    "revoke_authorization": ToolCatalogEntry(
        name="revoke_authorization",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.REVOKE_AUTHORIZATION,
        read_only=False,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="authorization_failed",
        notes="Revoke previously issued commit authorization.",
    ),
    "run_independent_review": ToolCatalogEntry(
        name="run_independent_review",
        owner_service="ReviewManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CANDIDATE_REVIEW,
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=True,
        error_family="review_failed",
        notes="Run independent reviewer model against candidate diff.",
    ),
    "run_runtime_verification": ToolCatalogEntry(
        name="run_runtime_verification",
        owner_service="RuntimeVerificationManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.RUNTIME_VERIFICATION,
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=True,
        error_family="verification_failed",
        notes="Execute policy-named runtime check against canonical repository.",
    ),
    "search_project_markdown": ToolCatalogEntry(
        name="search_project_markdown",
        owner_service="RepositoryReader",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.REPOSITORY_READ,
        read_only=True,
        destructive=False,
        idempotent=True,
        open_world=False,
        error_family="policy_denied",
        notes="Secondary integration: search text regex in project markdown files.",
    ),
    "send_to_local_dev": ToolCatalogEntry(
        name="send_to_local_dev",
        owner_service="DevelopmentJobManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.CLAUDE_EDIT,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
        error_family="job_lifecycle_error",
        notes="Spawn new isolated development worktree job.",
    ),
    "standalone_openhands_action": ToolCatalogEntry(
        name="standalone_openhands_action",
        owner_service="OpenHandsActionManager",
        stage=ToolLifecycleStage.QUARANTINED_PRE_V1,
        capability=ProjectCapability.GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
        error_family="runtime_error",
        notes="Quarantined pre-v1 tool: execute restricted OpenHands action in disposable runtime.",
    ),
    "sync_canonical_checkout": ToolCatalogEntry(
        name="sync_canonical_checkout",
        owner_service="SyncManager",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.SYNC_CANONICAL_CHECKOUT,
        read_only=False,
        destructive=True,
        idempotent=True,
        open_world=True,
        error_family="conflict_or_stale",
        notes="Secondary integration: fast-forward canonical checkout to upstream default.",
    ),
    "verify_development_job": ToolCatalogEntry(
        name="verify_development_job",
        owner_service="VerificationCommitManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.VERIFICATION_COMMIT,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
        error_family="verification_failed",
        notes="Run fixed verification command vectors against development worktree.",
    ),
    "working_tree_candidate_capture": ToolCatalogEntry(
        name="working_tree_candidate_capture",
        owner_service="WorkingTreeCaptureManager",
        stage=ToolLifecycleStage.CORE_V1,
        capability=ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE,
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        error_family="capture_failed",
        notes="Capture dirty working tree changes into candidate branch without modifying source.",
    ),
    "write_project_markdown": ToolCatalogEntry(
        name="write_project_markdown",
        owner_service="RepositoryWriter",
        stage=ToolLifecycleStage.SECONDARY_V1,
        capability=ProjectCapability.MARKDOWN_WRITE,
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=False,
        error_family="validation_failed",
        notes="Secondary integration: SHA-256 guarded write/update within approved roots.",
    ),
}


def compute_schema_digest(tools: Iterable[Any]) -> str:
    """Compute a deterministic SHA-256 digest over canonical JSON of tools.

    Sorts tools by name and serializes their names, parameter schemas, and
    ToolAnnotations to ensure reproducible schema fingerprints across runs.
    """
    items: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: getattr(t, "name", "")):
        ann = getattr(tool, "annotations", None)
        items.append(
            {
                "name": getattr(tool, "name", ""),
                "parameters": getattr(tool, "parameters", {}),
                "annotations": {
                    "readOnlyHint": (
                        bool(getattr(ann, "readOnlyHint", False)) if ann else False
                    ),
                    "destructiveHint": (
                        bool(getattr(ann, "destructiveHint", False)) if ann else False
                    ),
                    "idempotentHint": (
                        bool(getattr(ann, "idempotentHint", False)) if ann else False
                    ),
                    "openWorldHint": (
                        bool(getattr(ann, "openWorldHint", False)) if ann else False
                    ),
                },
            }
        )
    canonical_json = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


async def inspect_server_schema(server: Any) -> dict[str, Any]:
    """Inspect FastMCP server tool surface and return structured catalog metrics."""
    tools = await server.list_tools()
    schema_digest = compute_schema_digest(tools)

    core_v1_count = 0
    secondary_v1_count = 0
    quarantined_count = 0
    tools_dict: dict[str, dict[str, Any]] = {}

    for tool in sorted(tools, key=lambda t: getattr(t, "name", "")):
        name = getattr(tool, "name", "")
        entry = PUBLIC_TOOL_CATALOG.get(name)
        ann = getattr(tool, "annotations", None)
        ro = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        de = bool(getattr(ann, "destructiveHint", False)) if ann else False
        idemp = bool(getattr(ann, "idempotentHint", False)) if ann else False
        ow = bool(getattr(ann, "openWorldHint", False)) if ann else False

        stage_val = entry.stage.value if entry else "unknown"
        if entry:
            if entry.stage == ToolLifecycleStage.CORE_V1:
                core_v1_count += 1
            elif entry.stage == ToolLifecycleStage.SECONDARY_V1:
                secondary_v1_count += 1
            elif entry.stage == ToolLifecycleStage.QUARANTINED_PRE_V1:
                quarantined_count += 1

        tools_dict[name] = {
            "name": name,
            "stage": stage_val,
            "owner_service": entry.owner_service if entry else "unknown",
            "capability": (
                entry.capability.value if (entry and entry.capability) else None
            ),
            "read_only": ro,
            "destructive": de,
            "idempotent": idemp,
            "open_world": ow,
            "error_family": entry.error_family if entry else "unknown",
            "notes": entry.notes if entry else "",
            "parameters": getattr(tool, "parameters", {}),
        }

    return {
        "total_tools": len(tools),
        "core_v1_count": core_v1_count,
        "secondary_v1_count": secondary_v1_count,
        "quarantined_count": quarantined_count,
        "schema_digest": schema_digest,
        "tools": tools_dict,
    }


async def validate_server_schema_compatibility(server: Any) -> list[str]:
    """Verify server tool surface against PUBLIC_TOOL_CATALOG.

    Detects missing tools, unexpected tools, and annotation mismatches.
    Returns an empty list if compatible, or a list of discrepancy descriptions.
    """
    tools = await server.list_tools()
    server_tools = {getattr(tool, "name", ""): tool for tool in tools}
    errors: list[str] = []

    # Check for missing tools and annotation discrepancies
    for name, entry in sorted(PUBLIC_TOOL_CATALOG.items()):
        if name not in server_tools:
            errors.append(f"Missing expected tool from public catalog: {name}")
            continue
        tool = server_tools[name]
        ann = getattr(tool, "annotations", None)
        ro = bool(getattr(ann, "readOnlyHint", False)) if ann else False
        de = bool(getattr(ann, "destructiveHint", False)) if ann else False
        idemp = bool(getattr(ann, "idempotentHint", False)) if ann else False
        ow = bool(getattr(ann, "openWorldHint", False)) if ann else False

        if ro != entry.read_only:
            errors.append(
                f"Tool '{name}' annotation mismatch for readOnlyHint: "
                f"expected {entry.read_only}, got {ro}"
            )
        if de != entry.destructive:
            errors.append(
                f"Tool '{name}' annotation mismatch for destructiveHint: "
                f"expected {entry.destructive}, got {de}"
            )
        if idemp != entry.idempotent:
            errors.append(
                f"Tool '{name}' annotation mismatch for idempotentHint: "
                f"expected {entry.idempotent}, got {idemp}"
            )
        if ow != entry.open_world:
            errors.append(
                f"Tool '{name}' annotation mismatch for openWorldHint: "
                f"expected {entry.open_world}, got {ow}"
            )

    # Check for unexpected tools
    for name in sorted(server_tools.keys()):
        if name not in PUBLIC_TOOL_CATALOG:
            errors.append(f"Unexpected tool registered in server: {name}")

    return errors
