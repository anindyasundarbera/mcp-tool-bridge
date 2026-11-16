# Transport-neutral governance application core (A1.3.1)
"""Transport-neutral governance application core.

This module provides the central application composition root and service layer
for BridgeLab, decoupling all governance operations, capability checks, and
domain manager orchestration from FastMCP and any transport layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Literal

from .authorization import AuthorizationError, AuthorizationManager
from .claude import ClaudeExecutor
from .config import Settings
from .evidence import EvidenceBundleError, EvidenceBundleManager
from .git import GitCommandError, project_status
from .introspection import CanonicalizationResolver, effective_policy_view
from .jobs import DevelopmentJobError, DevelopmentJobManager
from .models import Effort, ProjectCapability, ProjectPolicy
from .open_hands_action import OpenHandsActionError, OpenHandsActionManager
from .pr import PullRequestError, PullRequestManager
from .reader import RepositoryReader
from .registry import ProjectLockManager, ProjectRegistry
from .review import ReviewError, ReviewManager
from .runtime_verification import (
    RuntimeVerificationError,
    RuntimeVerificationManager,
)
from .sync import SyncError, SyncManager
from .verification import (
    PublicationError,
    PublicationManager,
    VerificationCommitManager,
    VerificationError,
)
from .working_tree_capture import (
    WorkingTreeCaptureError,
    WorkingTreeCaptureManager,
)
from .writer import RepositoryWriter, WriteMode
from . import store
from .worktrees import GitWorktreeManager
from .executor_contract import ExecutorPort
from .identity import CallerPrincipal, resolve_caller_principal
from .transports import PeerTransportMetadata, TransportMode


@dataclass(frozen=True)
class RequestContext:
    caller_id: str = "local"
    transport: str = "direct"
    correlation_id: str | None = None
    principal: CallerPrincipal | None = None
    transport_mode: str | TransportMode = "stdio"

    def resolved_principal(self) -> CallerPrincipal:
        """Resolve the caller principal from explicit principal or transport metadata."""
        if self.principal is not None:
            return self.principal
        mode_val = self.transport_mode
        if isinstance(mode_val, TransportMode):
            mode_str = mode_val.value.lower()
        else:
            mode_str = str(mode_val or "stdio").lower()

        if mode_str in ("stdio", "direct"):
            peer: PeerTransportMetadata | None = PeerTransportMetadata(
                transport_mode=TransportMode.STDIO
            )
        elif mode_str in ("http", "local_http"):
            peer = PeerTransportMetadata(
                transport_mode=TransportMode.LOCAL_HTTP,
                remote_addr="127.0.0.1",
            )
        elif mode_str in ("ngrok_mtls", "mtls"):
            peer = PeerTransportMetadata(transport_mode=TransportMode.NGROK_MTLS)
        else:
            peer = None

        return resolve_caller_principal(
            peer,
            claimed_actor_label=self.caller_id,
            allow_legacy_unauthenticated=True,
        )


class InspectionSubService:
    def __init__(self, app: GovernanceApplication) -> None:
        self._app = app

    async def list_allowed_projects(
        self, context: RequestContext | None = None
    ) -> dict[str, object]:
        return await self._app.list_allowed_projects(context=context)

    async def get_project_status(
        self, project_id: str, context: RequestContext | None = None
    ) -> dict[str, object]:
        return await self._app.get_project_status(project_id, context=context)

    async def list_development_jobs(
        self,
        project_id: str,
        scope: Literal["all", "active", "terminal"] = "all",
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.list_development_jobs(
            project_id, scope=scope, limit=limit, context=context
        )

    async def get_effective_project_policy(
        self, project_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.get_effective_project_policy(project_id, context=context)

    async def resolve_candidate_canonicalization(
        self, project_id: str, commit_sha: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.resolve_candidate_canonicalization(
            project_id, commit_sha, context=context
        )


class RepositorySubService:
    def __init__(self, app: GovernanceApplication) -> None:
        self._app = app

    async def list_project_markdown(
        self,
        project_id: str,
        path: str = ".",
        max_depth: int = 3,
        max_results: int = 100,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.list_project_markdown(
            project_id, path=path, max_depth=max_depth, max_results=max_results, context=context
        )

    async def search_project_markdown(
        self,
        project_id: str,
        query: str,
        path: str = ".",
        max_results: int = 30,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.search_project_markdown(
            project_id, query=query, path=path, max_results=max_results, context=context
        )

    async def read_project_markdown(
        self, project_id: str, path: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.read_project_markdown(project_id, path, context=context)

    async def read_project_file_range(
        self,
        project_id: str,
        path: str,
        start_line: int,
        end_line: int,
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.read_project_file_range(
            project_id, path, start_line, end_line, expected_sha256, context=context
        )

    async def get_markdown_outline(
        self, project_id: str, path: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.get_markdown_outline(project_id, path, context=context)

    async def get_project_file_metadata(
        self, project_id: str, path: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.get_project_file_metadata(project_id, path, context=context)

    async def write_project_markdown(
        self,
        project_id: str,
        path: str,
        content: str,
        write_mode: WriteMode = "create",
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.write_project_markdown(
            project_id, path, content, write_mode, expected_sha256, context=context
        )


class JobsSubService:
    def __init__(self, app: GovernanceApplication, manager: DevelopmentJobManager) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def send_to_local_dev(
        self,
        project_id: str,
        text: str,
        effort: Effort | None = None,
        requested_base: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.send_to_local_dev(
            project_id=project_id,
            text=text,
            effort=effort,
            requested_base=requested_base,
            context=context,
        )

    async def get_development_job(
        self, project_id: str, job_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.get_development_job(project_id, job_id, context=context)

    async def resume_development_job(
        self,
        project_id: str,
        job_id: str,
        text: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.resume_development_job(
            project_id, job_id, text, effort=effort, context=context
        )

    async def cancel_development_job(
        self, project_id: str, job_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.cancel_development_job(project_id, job_id, context=context)

    async def pause_development_job(
        self, project_id: str, job_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.pause_development_job(project_id, job_id, context=context)

    async def extend_budget(
        self,
        project_id: str,
        job_id: str,
        slices: int = 1,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.extend_budget(
            project_id, job_id, slices=slices, reason=reason, context=context
        )

    async def narrow_scope(
        self,
        project_id: str,
        job_id: str,
        paths: list[str],
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.narrow_scope(
            project_id, job_id, paths=paths, reason=reason, context=context
        )

    async def replace_worker(
        self,
        project_id: str,
        job_id: str,
        text: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.replace_worker(
            project_id, job_id, text, effort=effort, context=context
        )

    async def discard_candidate(
        self,
        project_id: str,
        job_id: str,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.discard_candidate(
            project_id, job_id, reason=reason, context=context
        )

    async def revoke_authorization(
        self,
        project_id: str,
        authorization_id: str,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.revoke_authorization(
            project_id, authorization_id, reason=reason, context=context
        )


class VerificationSubService:
    def __init__(self, app: GovernanceApplication, manager: VerificationCommitManager) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def verify_development_job(
        self,
        project_id: str,
        job_id: str,
        expected_head: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.verify_development_job(
            project_id, job_id, expected_head, context=context
        )

    async def replay_verification(
        self, project_id: str, verification_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.replay_verification(
            project_id, verification_id, context=context
        )

    async def run_runtime_verification(
        self,
        project_id: str,
        check_id: str,
        expected_head: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.run_runtime_verification(
            project_id, check_id, expected_head, context=context
        )


class ReviewSubService:
    def __init__(self, app: GovernanceApplication, manager: ReviewManager) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def list_candidate_changes(
        self, project_id: str, job_id: str, context: RequestContext | None = None
    ) -> dict[str, Any]:
        return await self._app.list_candidate_changes(project_id, job_id, context=context)

    async def read_candidate_file(
        self,
        project_id: str,
        job_id: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.read_candidate_file(
            project_id, job_id, path, start_line, end_line, expected_sha256, context=context
        )

    async def record_candidate_review(
        self,
        project_id: str,
        job_id: str,
        verdict: dict[str, Any],
        reviewer_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.record_candidate_review(
            project_id, job_id, verdict, reviewer_id, context=context
        )

    async def run_independent_review(
        self,
        project_id: str,
        job_id: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.run_independent_review(
            project_id, job_id, effort, context=context
        )

    async def get_candidate_review(
        self,
        project_id: str,
        job_id: str,
        review_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.get_candidate_review(
            project_id, job_id, review_id, context=context
        )


class PublicationSubService:
    def __init__(self, app: GovernanceApplication, manager: PublicationManager) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def commit_verified_development_job(
        self,
        project_id: str,
        job_id: str,
        verification_id: str,
        expected_head: str,
        expected_diff_fingerprint: str,
        paths: list[str],
        message: str,
        review_id: str | None = None,
        authorization_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.commit_verified_development_job(
            project_id,
            job_id,
            verification_id,
            expected_head,
            expected_diff_fingerprint,
            paths,
            message,
            review_id=review_id,
            authorization_id=authorization_id,
            context=context,
        )

    async def push_detached_development_commit(
        self,
        project_id: str,
        job_id: str,
        commit_sha: str,
        branch_name: str,
        expected_base_head: str,
        expected_diff_fingerprint: str,
        authorization_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.push_detached_development_commit(
            project_id,
            job_id,
            commit_sha,
            branch_name,
            expected_base_head,
            expected_diff_fingerprint,
            authorization_id=authorization_id,
            context=context,
        )

    async def sync_canonical_checkout(
        self,
        project_id: str,
        expected_current_head: str,
        target_commit: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.sync_canonical_checkout(
            project_id, expected_current_head, target_commit, context=context
        )

    async def prepare_canonical_for_work(
        self,
        project_id: str,
        pull: bool = False,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.prepare_canonical_for_work(
            project_id, pull=pull, context=context
        )

    async def export_evidence_bundle(
        self,
        project_id: str,
        job_id: str,
        expected_candidate_fingerprint: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.export_evidence_bundle(
            project_id, job_id, expected_candidate_fingerprint, context=context
        )

    async def open_pull_request(
        self,
        project_id: str,
        job_id: str,
        base_head: str,
        fingerprint: str,
        title: str,
        draft: bool = True,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.open_pull_request(
            project_id, job_id, base_head, fingerprint, title, draft=draft, context=context
        )


class CaptureSubService:
    def __init__(self, app: GovernanceApplication, manager: WorkingTreeCaptureManager) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def preflight_working_tree_capture(
        self,
        project_id: str,
        include_paths: list[str],
        protected_exclude_paths: list[str],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.preflight_working_tree_capture(
            project_id,
            include_paths,
            protected_exclude_paths,
            expected_base_head=expected_base_head,
            expected_working_tree_fingerprint=expected_working_tree_fingerprint,
            context=context,
        )

    async def working_tree_candidate_capture(
        self,
        project_id: str,
        include_paths: list[str],
        protected_exclude_paths: list[str],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.working_tree_candidate_capture(
            project_id,
            include_paths,
            protected_exclude_paths,
            expected_base_head=expected_base_head,
            expected_working_tree_fingerprint=expected_working_tree_fingerprint,
            effort=effort,
            context=context,
        )

    async def standalone_openhands_action(
        self,
        project_id: str,
        action_params: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        return await self._app.standalone_openhands_action(
            project_id, action_params, context=context
        )


class RequirementsSubService:
    def __init__(self, app: GovernanceApplication, manager: Any) -> None:
        self._app = app
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)

    async def requirements_covered_summary(
        self, project: ProjectPolicy, context: RequestContext | None = None
    ) -> dict[str, Any]:
        if hasattr(self._manager, "requirements_covered_summary"):
            return await self._manager.requirements_covered_summary(project)
        return {}


class GovernanceApplication:
    """Transport-neutral container and composition root for all governed bridge operations."""

    def __init__(
        self,
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
        runtime: RuntimeVerificationManager | None = None,
        capture: WorkingTreeCaptureManager | None = None,
        requirements: Any = None,
        state_db: sqlite3.Connection | None = None,
        authorization: AuthorizationManager | None = None,
        open_hands: OpenHandsActionManager | None = None,
        claude_executor: ClaudeExecutor | None = None,
        executor_port: ExecutorPort | None = None,
    ) -> None:
        active_settings = settings or Settings.from_env()
        owns_state_db = False
        if state_db is not None:
            active_state_db = state_db
        elif registry is not None and registry._state_db is not None:
            active_state_db = registry._state_db
        else:
            active_state_db = store.open_state_db(
                GitWorktreeManager(active_settings, ()).ensure_state_root() / "bridge.db"
            )
            owns_state_db = True

        self._owns_db = owns_state_db
        self.owns_state_db = owns_state_db
        self.state_db = active_state_db
        self.settings = active_settings

        if registry is not None:
            active_registry = registry
            if getattr(active_registry, "_state_db", None) is None:
                active_registry._state_db = active_state_db
        else:
            active_registry = ProjectRegistry.load(
                active_settings.projects_config_path,
                require_apex_grounding=active_settings.require_apex_grounding,
                state_db=active_state_db,
            )
        self.registry = active_registry

        self.locks = locks or ProjectLockManager()
        active_claude = claude_executor or executor or ClaudeExecutor(active_settings)
        self.executor = active_claude
        self.reader = reader or RepositoryReader(active_settings)
        self.writer = writer or RepositoryWriter(active_settings)

        if requirements is not None:
            active_requirements = requirements
        else:
            from .requirement import RequirementManager

            active_requirements = RequirementManager(
                active_settings,
                store,
                self.reader,
                db=active_state_db,
            )
        self.requirements_manager = active_requirements

        active_jobs = jobs or DevelopmentJobManager(
            active_settings,
            self.locks,
            self.executor,
            projects=self.registry.projects(),
            requirements=active_requirements,
            state_db=active_state_db,
            executor_port=executor_port,
        )
        self.jobs_manager = active_jobs
        self.job_manager = active_jobs
        self.executor_port: ExecutorPort | None = getattr(active_jobs, "executor_port", None)

        active_verification = verification or VerificationCommitManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.verification_manager = active_verification

        active_review = review or ReviewManager(
            active_settings,
            self.locks,
            active_jobs,
            self.executor,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.review_manager = active_review

        active_publication = publication or PublicationManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.publication_manager = active_publication

        active_sync = sync or SyncManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.sync_manager = active_sync
        self.sync = active_sync

        active_authorization = authorization or AuthorizationManager(
            active_settings,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.authorization_manager = active_authorization
        self.authorization = active_authorization

        active_evidence = evidence or EvidenceBundleManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.evidence_manager = active_evidence
        self.evidence = active_evidence

        active_pr = pr or PullRequestManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.pr_manager = active_pr
        self.pr = active_pr

        active_canonicalization = canonicalization or CanonicalizationResolver(
            active_settings,
            self.locks,
            active_jobs,
            active_pr,
            active_sync,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.canonicalization_resolver = active_canonicalization
        self.canonicalization = active_canonicalization

        active_runtime = runtime or RuntimeVerificationManager(
            active_settings,
            self.locks,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.runtime_manager = active_runtime
        self.runtime = active_runtime

        active_capture = capture or WorkingTreeCaptureManager(
            active_settings,
            self.locks,
            active_jobs,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.capture_manager = active_capture

        active_open_hands = open_hands or OpenHandsActionManager(
            active_settings,
            self.locks,
            projects=self.registry.projects(),
            state_db=active_state_db,
        )
        self.open_hands_manager = active_open_hands
        self.open_hands = active_open_hands

        # Structured sub-services
        self.inspection = InspectionSubService(self)
        self.repository = RepositorySubService(self)
        self.jobs = JobsSubService(self, active_jobs)
        self.verification = VerificationSubService(self, active_verification)
        self.review = ReviewSubService(self, active_review)
        self.publication = PublicationSubService(self, active_publication)
        self.capture = CaptureSubService(self, active_capture)
        self.requirements = RequirementsSubService(self, active_requirements)

    # ------------------------------------------------------------------
    # Project discovery and read-only repository access
    # ------------------------------------------------------------------

    async def list_allowed_projects(
        self, context: RequestContext | None = None
    ) -> dict[str, object]:
        """List the project IDs this bridge is authorized to access."""
        return {"projects": self.registry.list_public()}

    async def get_project_status(
        self,
        project_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, object]:
        """Get Git branch, HEAD, cleanliness, and diff summary for one exact allowed project."""
        project = self.registry.resolve(
            project_id, ProjectCapability.REPOSITORY_READ
        )
        result: dict[str, object] = {
            "project_id": project.id,
            "project_name": project.display_name,
            "repository": await project_status(project.root),
        }
        try:
            req_manager = self.jobs_manager._requirements_manager()
            summary = await req_manager.requirements_covered_summary(project)
            if summary.get("normative", 0) > 0:
                result["requirements_covered"] = summary
        except Exception:
            pass
        return result

    async def list_project_markdown(
        self,
        project_id: str,
        path: str = ".",
        max_depth: int = 3,
        max_results: int = 100,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """List Markdown files beneath an allowed project directory without reading contents."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.list_markdown(project, path, max_depth, max_results)

    async def search_project_markdown(
        self,
        project_id: str,
        query: str,
        path: str = ".",
        max_results: int = 30,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Search Markdown files in an allowed project for a literal, case-insensitive text query."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.search(project, query, path, max_results)

    async def read_project_markdown(
        self,
        project_id: str,
        path: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read one complete UTF-8 Markdown file from an exact allowed project path."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.read_markdown(project, path)

    async def read_project_file_range(
        self,
        project_id: str,
        path: str,
        start_line: int,
        end_line: int,
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read a bounded line range from one Markdown file."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.read_range(
            project, path, start_line, end_line, expected_sha256
        )

    async def get_markdown_outline(
        self,
        project_id: str,
        path: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Return ATX and setext Markdown headings with levels and line numbers."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.outline(project, path)

    async def get_project_file_metadata(
        self,
        project_id: str,
        path: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Get SHA-256, byte size, line count, modified timestamp, and Git status."""
        project = self.registry.resolve(project_id, ProjectCapability.REPOSITORY_READ)
        return await self.reader.metadata(project, path)

    # ------------------------------------------------------------------
    # Markdown writer
    # ------------------------------------------------------------------

    async def write_project_markdown(
        self,
        project_id: str,
        path: str,
        content: str,
        write_mode: WriteMode = "create",
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Create or update one UTF-8 Markdown file beneath an approved write root."""
        project = self.registry.resolve(project_id, ProjectCapability.MARKDOWN_WRITE)
        lock = await self.locks.for_project(project.id)
        async with lock:
            return await self.writer.write(
                project, path, content, write_mode, expected_sha256
            )

    # ------------------------------------------------------------------
    # Development job lifecycle
    # ------------------------------------------------------------------

    async def send_to_local_dev(
        self,
        project_id: str,
        text: str,
        effort: Effort | None = None,
        requested_base: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Start an asynchronous Claude Code job in a detached Git worktree."""
        project = self.registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        return await self.jobs_manager.start(
            project, text, effort, requested_base, drafter_id="ChatGPT"
        )

    async def get_development_job(
        self,
        project_id: str,
        job_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Get the current status and bounded result of one bridge-created development job."""
        project = self.registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CLAUDE_EDIT
        )
        return await self.jobs_manager.get(project, job_id)

    async def resume_development_job(
        self,
        project_id: str,
        job_id: str,
        text: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Continue a terminal development job in its retained worktree."""
        project = self.registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CLAUDE_EDIT
        )
        return await self.jobs_manager.resume(project, job_id, text, effort)

    async def cancel_development_job(
        self,
        project_id: str,
        job_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Cancel one queued or running bridge-created development job and terminate its process."""
        project = self.registry.resolve(project_id, ProjectCapability.CLAUDE_EDIT)
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CLAUDE_EDIT
        )
        return await self.jobs_manager.cancel(project, job_id)

    # ------------------------------------------------------------------
    # M-CC-08 human control surface (dormant by default)
    # ------------------------------------------------------------------

    async def pause_development_job(
        self,
        project_id: str,
        job_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Pause a running development job at the next safe boundary."""
        project = self.registry.resolve(
            project_id, ProjectCapability.PAUSE_DEVELOPMENT_JOB
        )
        return await self.jobs_manager.pause(project, job_id)

    async def extend_budget(
        self,
        project_id: str,
        job_id: str,
        slices: int = 1,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Grant manual budget-extension slices to a development job paused at exhaustion."""
        project = self.registry.resolve(
            project_id, ProjectCapability.EXTEND_BUDGET
        )
        return await self.jobs_manager.extend_budget(
            project, job_id, slices=slices, reason=reason
        )

    async def narrow_scope(
        self,
        project_id: str,
        job_id: str,
        paths: list[str],
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Reduce a development job's allowed changed-path set."""
        project = self.registry.resolve(
            project_id, ProjectCapability.NARROW_SCOPE
        )
        return await self.jobs_manager.narrow_scope(
            project, job_id, paths=paths, reason=reason
        )

    async def replace_worker(
        self,
        project_id: str,
        job_id: str,
        text: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Re-spawn the drafter for a development job that is terminal."""
        project = self.registry.resolve(
            project_id, ProjectCapability.REPLACE_WORKER
        )
        return await self.jobs_manager.replace_worker(
            project, job_id, text, effort=effort
        )

    async def discard_candidate(
        self,
        project_id: str,
        job_id: str,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Permanently remove a development job's candidate worktree and clear its session."""
        project = self.registry.resolve(
            project_id, ProjectCapability.DISCARD_CANDIDATE
        )
        return await self.jobs_manager.discard_candidate(
            project, job_id, reason=reason
        )

    async def revoke_authorization(
        self,
        project_id: str,
        authorization_id: str,
        reason: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Revoke a previously granted authorization by marking it consumed."""
        project = self.registry.resolve(
            project_id, ProjectCapability.REVOKE_AUTHORIZATION
        )
        return await self.jobs_manager.revoke_authorization(
            project, authorization_id, reason=reason
        )

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def verify_development_job(
        self,
        project_id: str,
        job_id: str,
        expected_head: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Run only the fixed verification commands configured for a completed retained job."""
        project = self.registry.resolve(
            project_id, ProjectCapability.VERIFICATION_COMMIT
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.VERIFICATION_COMMIT
        )
        return await self.verification_manager.verify(project, job_id, expected_head)

    async def replay_verification(
        self,
        project_id: str,
        verification_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read-only deterministic replay of a recorded verification."""
        project = self.registry.resolve(
            project_id, ProjectCapability.REPLAY_VERIFICATION
        )
        try:
            return await self.verification_manager.replay(project, verification_id)
        except VerificationError as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Candidate review
    # ------------------------------------------------------------------

    async def list_candidate_changes(
        self,
        project_id: str,
        job_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """List the changed paths of a completed development job's retained candidate."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await self.review_manager.list_candidate_changes(project, job_id)

    async def read_candidate_file(
        self,
        project_id: str,
        job_id: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        expected_sha256: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read one file from a completed job's retained candidate."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await self.review_manager.read_candidate_file(
            project, job_id, path, start_line, end_line, expected_sha256
        )

    async def record_candidate_review(
        self,
        project_id: str,
        job_id: str,
        verdict: dict[str, Any],
        reviewer_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Record a reviewer's structured verdict for a completed job's candidate."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await self.review_manager.record_candidate_review(
            project, job_id, verdict, reviewer_id
        )

    async def run_independent_review(
        self,
        project_id: str,
        job_id: str,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Spawn a fresh, read-only Claude reviewer over a completed job's candidate."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await self.review_manager.run_independent_review(project, job_id, effort)

    async def get_candidate_review(
        self,
        project_id: str,
        job_id: str,
        review_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Retrieve a previously recorded candidate review verdict by its single-use review_id."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANDIDATE_REVIEW
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.CANDIDATE_REVIEW
        )
        return await self.review_manager.get_candidate_review(project, job_id, review_id)

    # ------------------------------------------------------------------
    # Commit, publication, and sync
    # ------------------------------------------------------------------

    async def commit_verified_development_job(
        self,
        project_id: str,
        job_id: str,
        verification_id: str,
        expected_head: str,
        expected_diff_fingerprint: str,
        paths: list[str],
        message: str,
        review_id: str | None = None,
        authorization_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """After separate human approval, create one detached commit from verified state."""
        project = self.registry.resolve(
            project_id, ProjectCapability.VERIFICATION_COMMIT
        )
        try:
            await self.jobs_manager.require_job_capability(
                project, job_id, ProjectCapability.VERIFICATION_COMMIT
            )
            if project.requires_review:
                if review_id is None:
                    raise ReviewError(
                        "review_id is required: this project requires a candidate "
                        "review before a verified commit"
                    )
                await self.review_manager.validate_for_commit(
                    project,
                    job_id,
                    review_id,
                    expected_head,
                    expected_diff_fingerprint,
                    paths,
                )
            if self.settings.enable_durable_authz and authorization_id is not None:
                self.authorization_manager.check(
                    authorization_id,
                    project_id=project.id,
                    capability=ProjectCapability.VERIFICATION_COMMIT,
                    target=verification_id,
                )
            committed = await self.verification_manager.commit(
                project,
                job_id,
                verification_id,
                expected_head,
                expected_diff_fingerprint,
                paths,
                message,
            )
            if self.settings.enable_durable_authz and authorization_id is not None:
                self.authorization_manager.consume(authorization_id)
            if project.requires_review and review_id is not None:
                try:
                    await self.review_manager.mark_used(review_id)
                except Exception:
                    committed = {
                        **committed,
                        "review_mark_warning": (
                            "commit succeeded but the review record could not be "
                            "marked used; the single-use verification_id still "
                            "prevents a second commit"
                        ),
                    }
            return committed
        except (VerificationError, ReviewError, AuthorizationError, DevelopmentJobError) as exc:
            raise ValueError(str(exc)) from exc

    async def push_detached_development_commit(
        self,
        project_id: str,
        job_id: str,
        commit_sha: str,
        branch_name: str,
        expected_base_head: str,
        expected_diff_fingerprint: str,
        authorization_id: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """After separate human approval, publish the exact detached commit to a new branch on origin."""
        project = self.registry.resolve(
            project_id, ProjectCapability.PUSH_DEVELOPMENT_COMMIT
        )
        try:
            await self.jobs_manager.require_job_capability(
                project, job_id, ProjectCapability.PUSH_DEVELOPMENT_COMMIT
            )
            if self.settings.enable_durable_authz and authorization_id is not None:
                self.authorization_manager.check(
                    authorization_id,
                    project_id=project.id,
                    capability=ProjectCapability.PUSH_DEVELOPMENT_COMMIT,
                    target=branch_name,
                )
            published = await self.publication_manager.publish(
                project,
                job_id,
                commit_sha,
                branch_name,
                expected_base_head,
                expected_diff_fingerprint,
            )
            if self.settings.enable_durable_authz and authorization_id is not None:
                self.authorization_manager.consume(authorization_id)
            return published
        except (PublicationError, AuthorizationError, DevelopmentJobError) as exc:
            raise ValueError(str(exc)) from exc

    async def sync_canonical_checkout(
        self,
        project_id: str,
        expected_current_head: str,
        target_commit: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Fast-forward canonical checkout to verified remote tip."""
        project = self.registry.resolve(
            project_id, ProjectCapability.SYNC_CANONICAL_CHECKOUT
        )
        try:
            return await self.sync_manager.sync(project, expected_current_head, target_commit)
        except SyncError as exc:
            raise ValueError(str(exc)) from exc

    async def prepare_canonical_for_work(
        self,
        project_id: str,
        pull: bool = False,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Pre-work canonical-checkout update check and fast-forward."""
        project = self.registry.resolve(
            project_id, ProjectCapability.SYNC_CANONICAL_CHECKOUT
        )
        if not pull:
            return await self.sync_manager.check_for_updates(project)
        try:
            return await self.sync_manager.sync(
                project,
                expected_current_head=None,
                target_commit=None,
            )
        except SyncError as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Evidence, PR, and introspection
    # ------------------------------------------------------------------

    async def export_evidence_bundle(
        self,
        project_id: str,
        job_id: str,
        expected_candidate_fingerprint: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Assemble a single redacted, content-addressed evidence bundle for one development job."""
        project = self.registry.resolve(
            project_id, ProjectCapability.EXPORT_EVIDENCE_BUNDLE
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.EXPORT_EVIDENCE_BUNDLE
        )
        try:
            return await self.evidence_manager.export_bundle(
                project,
                job_id,
                expected_candidate_fingerprint,
            )
        except EvidenceBundleError as exc:
            raise ValueError(str(exc)) from exc

    async def open_pull_request(
        self,
        project_id: str,
        job_id: str,
        base_head: str,
        fingerprint: str,
        title: str,
        draft: bool = True,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Open or update a draft pull request for a development job's published remote branch."""
        project = self.registry.resolve(
            project_id, ProjectCapability.OPEN_PULL_REQUEST
        )
        await self.jobs_manager.require_job_capability(
            project, job_id, ProjectCapability.OPEN_PULL_REQUEST
        )
        try:
            return await self.pr_manager.open_pull_request(
                project,
                job_id,
                base_head,
                fingerprint,
                title,
                draft=draft,
            )
        except PullRequestError as exc:
            raise ValueError(str(exc)) from exc

    async def list_development_jobs(
        self,
        project_id: str,
        scope: Literal["all", "active", "terminal"] = "all",
        limit: int = 50,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Enumerate this project's durable development jobs without a job ID."""
        project = self.registry.resolve(
            project_id, ProjectCapability.JOB_INTROSPECTION
        )
        return await self.jobs_manager.list_jobs(project, scope, limit)

    async def get_effective_project_policy(
        self,
        project_id: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Expose the validated effective policy for one allowed project."""
        project = self.registry.resolve(
            project_id, ProjectCapability.POLICY_INTROSPECTION
        )
        redaction_paths = (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *(policy.root for policy in self.registry.projects()),
        )
        return effective_policy_view(project, redaction_paths)

    async def resolve_candidate_canonicalization(
        self,
        project_id: str,
        commit_sha: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Reconcile a detached development commit against canonical history."""
        project = self.registry.resolve(
            project_id, ProjectCapability.CANONICAL_HISTORY_READ
        )
        try:
            return await self.canonicalization_resolver.resolve(project, commit_sha)
        except (GitCommandError, RuntimeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Runtime verification and working-tree capture
    # ------------------------------------------------------------------

    async def run_runtime_verification(
        self,
        project_id: str,
        check_id: str,
        expected_head: str,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Run one named, policy-authorized runtime verification check."""
        project = self.registry.resolve(
            project_id, ProjectCapability.RUNTIME_VERIFICATION
        )
        try:
            return await self.runtime_manager.run(project, check_id, expected_head)
        except RuntimeVerificationError as exc:
            raise ValueError(str(exc)) from exc

    async def preflight_working_tree_capture(
        self,
        project_id: str,
        include_paths: list[str],
        protected_exclude_paths: list[str],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Read-only preflight for working_tree_candidate_capture."""
        project = self.registry.resolve(
            project_id, ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE
        )
        try:
            return await self.capture_manager.preflight(
                project,
                include_paths,
                protected_exclude_paths,
                expected_base_head=expected_base_head,
                expected_working_tree_fingerprint=expected_working_tree_fingerprint,
            )
        except WorkingTreeCaptureError as exc:
            raise ValueError(str(exc)) from exc

    async def working_tree_candidate_capture(
        self,
        project_id: str,
        include_paths: list[str],
        protected_exclude_paths: list[str],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
        effort: Effort | None = None,
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Capture an approved set of uncommitted working-tree changes into a retained candidate."""
        project = self.registry.resolve(
            project_id, ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE
        )
        try:
            return await self.capture_manager.capture(
                project,
                include_paths,
                protected_exclude_paths,
                expected_base_head=expected_base_head,
                expected_working_tree_fingerprint=expected_working_tree_fingerprint,
                effort=effort,
            )
        except WorkingTreeCaptureError as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Governed disposable runtime action execution
    # ------------------------------------------------------------------

    async def standalone_openhands_action(
        self,
        project_id: str,
        action_params: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Execute a restricted OpenHands action in an isolated disposable runtime."""
        project = self.registry.resolve(
            project_id,
            ProjectCapability.GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION,
        )
        try:
            return await self.open_hands_manager.execute_action(project, action_params)
        except OpenHandsActionError as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Lifecycle & Context Managers
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close all managers and the shared state DB (when owned)."""
        managers = [
            self.registry,
            self.jobs_manager,
            self.verification_manager,
            self.review_manager,
            self.publication_manager,
            self.sync_manager,
            self.authorization_manager,
            self.evidence_manager,
            self.pr_manager,
            self.canonicalization_resolver,
            self.runtime_manager,
            self.capture_manager,
            self.open_hands_manager,
            self.requirements_manager,
        ]
        for mgr in managers:
            if mgr is not None and hasattr(mgr, "close") and callable(mgr.close):
                try:
                    mgr.close()
                except Exception:
                    pass
        if self.state_db is not None and (self._owns_db or self.owns_state_db):
            try:
                self.state_db.close()
            except Exception:
                pass

    def close_managers(self) -> None:
        """Alias for :meth:`close`."""
        self.close()

    def __enter__(self) -> GovernanceApplication:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> GovernanceApplication:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
