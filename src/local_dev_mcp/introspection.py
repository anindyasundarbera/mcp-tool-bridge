"""Read-only introspection for the local development bridge (M-AL-01).

Two surfaces live here:

* ``effective_policy_view`` — expose the validated effective ``ProjectPolicy`` for
  a project as a deterministic, redacted dict with a ``policy_fingerprint``. It
  reuses the already-validated frozen policy from the registry (it does NOT
  re-parse JSON) and redacts the absolute root.

* ``CanonicalizationResolver`` — given a 40-hex ``commit_sha``, reconcile a
  candidate detached commit against the project's canonical history through
  layered evidence (institutional records -> local git -> remote read -> canonical
  ancestry) and return a three-state verdict: ``CANONICAL`` (the commit is in the
  origin default-branch history, by direct ancestry or squash/rebase
  content-equivalence), ``NOT_CANONICAL`` (provenance links the commit to the
  project and it is published but not yet absorbed into canonical history), or
  ``INDETERMINATE`` (provenance missing, content mismatch, or the canonical tip
  could not be read).

The resolver never mutates: it issues only read-only git (``rev-parse``,
``merge-base --is-ancestor``, ``rev-parse <tree>``, ``log --first-parent``,
``ls-tree -r -z``, and ``ls-remote``/``symbolic-ref`` via the existing
read-only helpers) and read-only
``gh`` (``pr list``/``pr view`` via ``PullRequestManager``). It never runs
``git fetch`` (which would mutate ``.git`` object/ref state), never pushes,
merges, tags, moves a ref, or modifies the canonical checkout. It opens its own
read-only connection to ``bridge.db`` for durable-record lookups, mirroring the
other managers; job state comes from the lock-guarded
``DevelopmentJobManager.committed_job_summary`` accessor.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import store
from .config import Settings
from .git import GitCommandError, run_git
from .jobs import DevelopmentJobManager
from .models import ProjectPolicy
from .pr import PullRequestError, PullRequestManager
from .registry import ProjectLockManager
from .sync import SyncManager
from .verification import (
    PublicationError,
    VerificationError,
    _paths_from_verification_row,
    _redact,
    _remote_branch_sha,
    committed_state,
)
from .worktrees import GitWorktreeManager, JobWorktree

# Bounded depth for the squash/rebase content-equivalence scan: we compare the
# candidate commit's tree against the trees of the most recent
# ``_SQUASH_SEARCH_DEPTH`` first-parent commits on the canonical default branch.
# Bounded so the read-only tool never scans unbounded history.
_SQUASH_SEARCH_DEPTH = 100

_VERDICTS = ("CANONICAL", "NOT_CANONICAL", "INDETERMINATE")


def effective_policy_view(
    project: ProjectPolicy,
    redaction_paths: tuple[Any, ...],
) -> dict[str, Any]:
    """Build a deterministic, redacted view of the validated effective policy.

    Reuses the frozen ``ProjectPolicy`` the registry already validated (it does
    NOT re-parse JSON) and exposes the safe, ordered fields an orchestrator needs
    to reason about the project's authorization surface. The absolute ``root`` is
    never returned (``root_redacted`` is always true); all ordered collections
    (capabilities, roots, paths) are sorted deterministically, and the
    ``policy_fingerprint`` is a SHA-256 over the canonical JSON of every exposed
    field (minus the fingerprint itself) so a caller can detect any change.
    """
    view: dict[str, Any] = {
        "project_id": project.id,
        "display_name": project.display_name,
        "description": project.description,
        "enabled": project.enabled,
        "require_git": project.require_git,
        "capabilities": sorted(capability.value for capability in project.capabilities),
        "markdown_write_roots": sorted(project.markdown_write_roots),
        "commit_roots": sorted(project.commit_roots),
        "protected_paths": sorted(project.protected_paths),
        "verification_commands": [
            command.model_dump(mode="json")
            for command in sorted(project.verification_commands, key=lambda c: c.id)
        ],
        "supply_chain_scans": [
            scan.model_dump(mode="json")
            for scan in sorted(project.supply_chain_scans, key=lambda s: s.id)
        ],
        # MCPB-011: runtime verification commands are exposed sanitized — the
        # check IDs, execution class, and (redacted) argv reveal the configured
        # runtime-check surface, but environment values are always replaced
        # with "<redacted>" so no secret, token, or host path leaks via
        # introspection. Keys stay visible so a caller can see which env vars a
        # check consumes without learning their values.
        "runtime_verification_commands": [
            {
                "id": command.id,
                "execution_mode": command.execution_mode,
                "argv": list(command.argv),
                "has_environment": bool(command.environment),
                "environment": {
                    key: "<redacted>" for key in sorted(command.environment)
                },
                "timeout_seconds": command.timeout_seconds,
            }
            for command in sorted(
                project.runtime_verification_commands, key=lambda c: c.id
            )
        ],
        # MCPB-012: the approved working-tree capture scope is exposed so an
        # orchestrator can echo the exact approved include/protected-exclude
        # paths back into the capture tools. The scope is pinned in trusted
        # local policy and is the only set the capture tools will accept.
        "working_tree_capture_scope": (
            {
                "include_paths": sorted(project.working_tree_capture_scope.include_paths),
                "protected_exclude_paths": sorted(
                    project.working_tree_capture_scope.protected_exclude_paths
                ),
            }
            if project.working_tree_capture_scope is not None
            else None
        ),
        # M-CC-11 security-hardening policies, exposed so an orchestrator can
        # observe which hardening a project opted into. Values are never
        # secrets: allowlist entries and credential names are identifiers.
        "filesystem_policy": (
            {
                "read_allow": sorted(project.filesystem_policy.read_allow),
                "write_allow": sorted(project.filesystem_policy.write_allow),
            }
            if project.filesystem_policy is not None
            else None
        ),
        "credential_policy": (
            {"allow": sorted(project.credential_policy.allow)}
            if project.credential_policy is not None
            else None
        ),
        "signed_manifests": project.signed_manifests,
        "sensitivity_policy": (
            {
                "sensitive_paths": sorted(project.sensitivity_policy.sensitive_paths),
            }
            if project.sensitivity_policy is not None
            else None
        ),
        "default_effort": project.default_effort,
        "allow_ultracode": project.allow_ultracode,
        "self_managed": project.self_managed,
        "requires_review": project.requires_review,
        "allow_bridge_reviewer": project.allow_bridge_reviewer,
        "root_redacted": True,
    }
    if project.claude_model is not None:
        view["claude_model"] = project.claude_model
    if project.claude_max_turns is not None:
        view["claude_max_turns"] = project.claude_max_turns
    fingerprint_payload = {k: v for k, v in view.items() if k != "root_redacted"}
    view["policy_fingerprint"] = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return _redact(view, redaction_paths)


class CanonicalizationResolver:
    """Reconcile a candidate detached commit against canonical history (M-AL-01).

    Read-only and origin-only: never fetches, pushes, merges, tags, moves a ref,
    or modifies the canonical checkout. See the module docstring for the layered
    evidence model and the three-state verdict.
    """

    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        jobs: DevelopmentJobManager,
        pr: PullRequestManager,
        sync: SyncManager,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._jobs = jobs
        self._pr = pr
        self._sync = sync
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._project_roots = tuple(project.root for project in projects)
        # Own read-only connection to bridge.db for durable-record lookups, the
        # same way every manager co-owns the shared state database.
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")

    def _redaction_paths(self, project: ProjectPolicy) -> tuple[Any, ...]:
        return (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def resolve(
        self,
        project: ProjectPolicy,
        commit_sha: str,
    ) -> dict[str, Any]:
        """Reconcile ``commit_sha`` against the project's canonical history."""
        commit_sha = commit_sha.lower()
        timeout = self.settings.git_command_timeout_seconds
        root = project.root

        commit_valid = await self._commit_present(root, commit_sha, timeout)
        provenance = await self._provenance(project, commit_sha)
        content_check = await self._content_check(
            project, root, commit_sha, provenance, timeout
        )
        # Branch/PR are resolved before canonical ancestry so the PR landing SHA
        # is available to Layer 4: ``landing_is_ancestor`` is a distinct fact from
        # ``candidate_is_ancestor`` and the two must never be collapsed.
        remote_branch, pull_request = await self._branch_and_pr(
            root, provenance, timeout
        )
        landing_sha = (
            pull_request.get("merge_commit_sha") if pull_request else None
        )
        canonical = await self._canonical_ancestry(
            root, commit_sha, landing_sha, timeout
        )
        content_equivalence = await self._content_equivalence(
            root, commit_sha, provenance, canonical, timeout
        )
        audit_trail = self._audit_view(project.id)

        verdict, reason, method, warnings = self._decide(
            commit_valid,
            provenance,
            content_check,
            canonical,
            content_equivalence,
            pull_request,
        )

        result: dict[str, Any] = {
            "project_id": project.id,
            "commit_sha": commit_sha,
            "verdict": verdict,
            "canonicalization_method": method,
            "reason": reason,
            "warnings": warnings,
            "commit_present_locally": commit_valid,
            "provenance": {
                "job_present": provenance["job_present"],
                "job_id": provenance["job_id"],
                "job_status": provenance["job_status"],
                "base_head": provenance["base_head"],
                "verification_present": provenance["verification_present"],
                "verification_id": provenance["verification_id"],
                "diff_fingerprint": provenance["diff_fingerprint"],
                "used_verification": provenance["used_verification"],
                "publication_present": provenance["publication_present"],
                "publication_branch": provenance["publication_branch"],
                "published_at": provenance["published_at"],
            },
            "content_check": content_check,
            "content_equivalence": content_equivalence,
            "canonical": canonical,
            "remote_branch": remote_branch,
            "pull_request": pull_request,
            "audit_trail": audit_trail,
        }
        return _redact(result, self._redaction_paths(project))

    async def _commit_present(
        self, root: Path, commit_sha: str, timeout: int
    ) -> bool:
        """True if ``commit_sha`` resolves to a commit object in the local store."""
        try:
            resolved = await run_git(
                root,
                "rev-parse",
                "--verify",
                f"{commit_sha}^{{commit}}",
                timeout_seconds=timeout,
            )
        except GitCommandError:
            return False
        return resolved.lower() == commit_sha

    async def _provenance(
        self, project: ProjectPolicy, commit_sha: str
    ) -> dict[str, Any]:
        """Layer 1: durable institutional records linking ``commit_sha`` to the project."""
        job_summary = await self._jobs.committed_job_summary(project.id, commit_sha)
        job_present = job_summary is not None
        job_id = job_summary["job_id"] if job_summary else None
        base_head = job_summary["base_head"] if job_summary else None
        job_status = job_summary["status"] if job_summary else None

        verification: dict[str, Any] | None = None
        if job_id is not None:
            rows = store.load_record_rows_for_job(self._db, job_id)
            used = [row for row in rows if row.get("used")]
            verification = used[0] if used else (rows[0] if rows else None)

        paths: tuple[str, ...] = ()
        if verification is not None:
            try:
                paths = _paths_from_verification_row(verification)
            except PublicationError:
                paths = ()

        publication: dict[str, Any] | None = None
        for row in store.load_publication_rows(self._db):
            row_sha = str(row.get("commit_sha", "")).lower()
            if row.get("project_id") == project.id and row_sha == commit_sha:
                publication = row
                break

        return {
            "job_present": job_present,
            "job_id": job_id,
            "job_status": job_status,
            "base_head": base_head,
            "verification_present": verification is not None,
            "verification_id": (
                verification.get("verification_id") if verification else None
            ),
            "diff_fingerprint": (
                verification.get("diff_fingerprint") if verification else None
            ),
            "used_verification": bool(verification.get("used")) if verification else False,
            "paths": paths,
            "publication_present": publication is not None,
            "publication_branch": (
                publication.get("branch_name") if publication else None
            ),
            "published_at": publication.get("published_at") if publication else None,
            "_verification_row": verification,
        }

    async def _content_check(
        self,
        project: ProjectPolicy,
        root: Path,
        commit_sha: str,
        provenance: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        """Layer 2: re-derive the candidate fingerprint from the commit object.

        ``committed_state`` reads the commit's tree with read-only ``git ls-tree``
        /``cat-file``. Worktrees share the canonical checkout's object store, so the
        check runs from ``project.root`` (no worktree needed). The re-derived
        fingerprint must equal the verification record's ``diff_fingerprint``;
        a mismatch means the commit object does not realize the verified candidate.
        """
        verification = provenance["_verification_row"]
        base_head = provenance["base_head"]
        paths = provenance["paths"]
        verified_fp = (
            verification.get("diff_fingerprint") if verification else None
        )
        if verification is None or base_head is None or not paths:
            return {
                "status": "skipped",
                "rederived_fingerprint": None,
                "verified_diff_fingerprint": verified_fp,
            }
        try:
            # A synthetic worktree rooted at the canonical checkout: worktrees
            # share the object store, so read-only ls-tree/cat-file from
            # project.root can read the committed object and its base tree.
            synthetic = JobWorktree(path=root, base_head=base_head)
            rederived = await committed_state(
                self.settings, project, synthetic, commit_sha, base_head, paths
            )
        except VerificationError:
            return {
                "status": "unavailable",
                "rederived_fingerprint": None,
                "verified_diff_fingerprint": verified_fp,
            }
        rederived_fp = rederived.fingerprint
        if rederived_fp.lower() == str(verified_fp or "").lower():
            return {
                "status": "matched",
                "rederived_fingerprint": rederived_fp,
                "verified_diff_fingerprint": verified_fp,
            }
        return {
            "status": "mismatched",
            "rederived_fingerprint": rederived_fp,
            "verified_diff_fingerprint": verified_fp,
        }

    async def _canonical_ancestry(
        self,
        root: Path,
        commit_sha: str,
        landing_sha: str | None,
        timeout: int,
    ) -> dict[str, Any]:
        """Layer 4: is the commit in the origin default-branch history?

        Origin-only and read-only: the default branch name comes from the local
        ``origin/HEAD`` symref (``symbolic-ref``), and the remote tip comes from
        ``ls-remote`` (no fetch, no ``.git`` mutation). Two ancestry facts are
        computed and kept distinct (never collapsed): ``candidate_is_ancestor``
        (the candidate commit itself is reachable from the canonical tip) and
        ``landing_is_ancestor`` (the PR merge/landing SHA is reachable from the
        canonical tip). Squash/rebase content-equivalence is tested by comparing
        the commit's tree against the recent first-parent history of the tip.
        """
        from .verification import _remote_default_branch

        default_branch = await _remote_default_branch(root, timeout_seconds=timeout)
        if default_branch is None:
            return self._canonical_dict(
                default_branch=None,
                current_head=None,
                candidate_is_ancestor=False,
                landing_sha=landing_sha,
                landing_is_ancestor=None,
                landing_present_locally=None,
                content_equivalent=False,
                matched_canonical_commit=None,
                status="default_unresolved",
            )
        try:
            remote_tip = await _remote_branch_sha(
                root, default_branch, timeout_seconds=timeout
            )
        except (GitCommandError, VerificationError):
            remote_tip = None
        if remote_tip is None:
            return self._canonical_dict(
                default_branch=default_branch,
                current_head=None,
                candidate_is_ancestor=False,
                landing_sha=landing_sha,
                landing_is_ancestor=None,
                landing_present_locally=None,
                content_equivalent=False,
                matched_canonical_commit=None,
                status="remote_unreachable",
            )
        # The live remote tip SHA from ls-remote is not guaranteed to be in the
        # local object store, and a read-only tool may not fetch it. If the tip
        # object is absent locally, ancestry and tree-equivalence checks against
        # it cannot run (merge-base / log would fail to resolve it and the
        # GitCommandError is swallowed into a false "not_in_history"). Surface
        # this as a distinct status so the decision maps it to INDETERMINATE
        # rather than a false NOT_CANONICAL for a genuinely canonical candidate.
        if not await self._commit_present(root, remote_tip, timeout):
            return self._canonical_dict(
                default_branch=default_branch,
                current_head=remote_tip,
                candidate_is_ancestor=False,
                landing_sha=landing_sha,
                landing_is_ancestor=None,
                landing_present_locally=None,
                content_equivalent=False,
                matched_canonical_commit=None,
                status="remote_tip_not_local",
            )
        candidate_anc = await self._is_ancestor(root, commit_sha, remote_tip, timeout)
        landing_anc, landing_present = await self._landing_ancestry(
            root, landing_sha, remote_tip, timeout
        )
        if candidate_anc:
            return self._canonical_dict(
                default_branch=default_branch,
                current_head=remote_tip,
                candidate_is_ancestor=True,
                landing_sha=landing_sha,
                landing_is_ancestor=landing_anc,
                landing_present_locally=landing_present,
                content_equivalent=False,
                matched_canonical_commit=None,
                status="ancestor",
            )
        equivalent, matched = await self._tree_equivalence(
            root, commit_sha, remote_tip, timeout
        )
        return self._canonical_dict(
            default_branch=default_branch,
            current_head=remote_tip,
            candidate_is_ancestor=False,
            landing_sha=landing_sha,
            landing_is_ancestor=landing_anc,
            landing_present_locally=landing_present,
            content_equivalent=equivalent,
            matched_canonical_commit=matched,
            status="content_equivalent" if equivalent else "not_in_history",
        )

    @staticmethod
    def _canonical_dict(
        *,
        default_branch: str | None,
        current_head: str | None,
        candidate_is_ancestor: bool,
        landing_sha: str | None,
        landing_is_ancestor: bool | None,
        landing_present_locally: bool | None,
        content_equivalent: bool,
        matched_canonical_commit: str | None,
        status: str,
    ) -> dict[str, Any]:
        return {
            "default_branch": default_branch,
            "current_head": current_head,
            "candidate_is_ancestor": candidate_is_ancestor,
            "landing_sha": landing_sha,
            "landing_is_ancestor": landing_is_ancestor,
            "landing_present_locally": landing_present_locally,
            "content_equivalent": content_equivalent,
            "matched_canonical_commit": matched_canonical_commit,
            "status": status,
        }

    async def _landing_ancestry(
        self,
        root: Path,
        landing_sha: str | None,
        canonical_tip: str,
        timeout: int,
    ) -> tuple[bool | None, bool | None]:
        """Ancestry of the PR landing SHA, kept distinct from the candidate's.

        Returns ``(landing_is_ancestor, landing_present_locally)``. When no
        landing SHA is known, both are ``None``. When the landing SHA is known but
        not present in the local object store (and a fetch is forbidden),
        ``landing_present_locally`` is ``False`` and ancestry is ``None`` so the
        decision can surface an INDETERMINATE "landing SHA unavailable" verdict
        rather than a false NOT_CANONICAL.
        """
        if landing_sha is None:
            return None, None
        present = await self._commit_present(root, landing_sha, timeout)
        if not present:
            return None, False
        return (
            await self._is_ancestor(root, landing_sha, canonical_tip, timeout),
            True,
        )

    async def _is_ancestor(
        self, root: Path, ancestor: str, descendant: str, timeout: int
    ) -> bool:
        """True if ``ancestor`` is an ancestor of (or equal to) ``descendant``."""
        try:
            await run_git(
                root,
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
                timeout_seconds=timeout,
            )
            return True
        except GitCommandError:
            return False

    async def _tree_equivalence(
        self, root: Path, commit_sha: str, canonical_tip: str, timeout: int
    ) -> tuple[bool, str | None]:
        """Squash/rebase content-equivalence: does the commit's tree appear on canonical history?

        Compares ``commit_sha``'s tree against the trees of the most recent
        ``_SQUASH_SEARCH_DEPTH`` first-parent commits reachable from the canonical
        tip. A squash merge of a single-commit PR produces a new commit on the
        default branch whose tree equals the original commit's tree, so this
        detects canonicalization even when the original commit is not an ancestor.
        Bounded so the read-only tool never scans unbounded history.
        """
        try:
            commit_tree = (
                await run_git(
                    root,
                    "rev-parse",
                    f"{commit_sha}^{{tree}}",
                    timeout_seconds=timeout,
                )
            ).strip()
            log_output = await run_git(
                root,
                "log",
                "--first-parent",
                f"-n{_SQUASH_SEARCH_DEPTH}",
                "--format=%H %T",
                canonical_tip,
                timeout_seconds=timeout,
            )
        except GitCommandError:
            return False, None
        for line in log_output.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == commit_tree:
                return True, parts[0]
        return False, None

    async def _content_equivalence(
        self,
        root: Path,
        commit_sha: str,
        provenance: dict[str, Any],
        canonical: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        """Layer 2.5: is the candidate's changed-path content present at landing?

        Squash/rebase merges land the candidate's content as a DIFFERENT commit,
        so the candidate SHA is not an ancestor even though its substantive
        content was incorporated. This compares the ``(mode, blob)`` state of
        every changed path in the verification record between the candidate
        commit and a landing commit on canonical history (the tree-matched
        commit from Layer 4, or the PR landing SHA when it is an ancestor).
        Read-only ``git ls-tree -r -z``; handles added/modified/deleted paths and
        executable-bit (mode) changes. Renames are covered when both the old and
        new paths are in the changed-path set. Fails closed: a mismatch on any
        path means the candidate's content did not land as-is, so the caller must
        not report CANONICAL on this evidence.
        """
        paths = provenance["paths"]
        if not paths:
            return {
                "status": "not_checked",
                "checked_paths": [],
                "mismatches": [],
                "landing_commit": None,
            }
        landing = canonical.get("matched_canonical_commit")
        if landing is None and canonical.get("landing_is_ancestor") is True:
            landing = canonical.get("landing_sha")
        if landing is None:
            return {
                "status": "not_checked",
                "checked_paths": list(paths),
                "mismatches": [],
                "landing_commit": None,
            }
        try:
            candidate_entries = await self._tree_entries(
                root, commit_sha, paths, timeout
            )
            landing_entries = await self._tree_entries(
                root, landing, paths, timeout
            )
        except GitCommandError:
            return {
                "status": "unavailable",
                "checked_paths": list(paths),
                "mismatches": [],
                "landing_commit": landing,
                "error": "landing commit tree could not be read locally",
            }
        mismatches = [
            path
            for path in paths
            if candidate_entries.get(path) != landing_entries.get(path)
        ]
        return {
            "status": "equivalent" if not mismatches else "mismatch",
            "checked_paths": list(paths),
            "mismatches": mismatches,
            "landing_commit": landing,
        }

    async def _tree_entries(
        self,
        root: Path,
        commit: str,
        paths: tuple[str, ...],
        timeout: int,
    ) -> dict[str, tuple[str, str]]:
        """Read ``(mode, blob)`` for each path at ``commit`` via ``ls-tree -r -z``.

        A path absent from the commit's tree (e.g. a path the candidate deleted)
        is simply absent from the returned dict, so a deleted-on-both comparison
        is an equality of ``None``. Bounded by the (already-bounded) path set.
        """
        output = await run_git(
            root,
            "ls-tree",
            "-r",
            "-z",
            commit,
            "--",
            *paths,
            timeout_seconds=timeout,
        )
        entries: dict[str, tuple[str, str]] = {}
        if not output:
            return entries
        for raw in output.split("\x00"):
            if not raw:
                continue
            meta, sep, path = raw.partition("\t")
            if not sep:
                continue
            parts = meta.split()
            if len(parts) >= 3 and path:
                entries[path] = (parts[0], parts[2])
        return entries

    async def _branch_and_pr(
        self,
        root: Path,
        provenance: dict[str, Any],
        timeout: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Layer 3: published remote branch state + associated pull request.

        Only populated when a publication record names a branch. Remote branch
        state is read-only ``ls-remote``; PR state is read-only ``gh pr list`` /
        ``gh pr view`` via ``PullRequestManager``. Both degrade gracefully: a
        remote/gh failure is surfaced as an error field rather than crashing the
        reconciliation.
        """
        branch_name = provenance["publication_branch"]
        if branch_name is None:
            return None, None

        remote_branch: dict[str, Any]
        try:
            state = await self._pr._remote_branch_state(
                root, branch_name, timeout=timeout
            )
            remote_branch = {
                "branch_name": state["branch_name"],
                "present": state["present"],
                "head_sha": state["head_sha"],
            }
        except (GitCommandError, VerificationError, PullRequestError):
            remote_branch = {
                "branch_name": branch_name,
                "present": False,
                "head_sha": None,
                "error": "remote branch read failed",
            }

        pull_request: dict[str, Any]
        try:
            discovered = await self._pr._discover_pr(root, branch_name, timeout=timeout)
        except PullRequestError:
            pull_request = {
                "present": None,
                "number": None,
                "state": None,
                "merged": None,
                "merge_commit_sha": None,
                "base_ref": None,
                "url": None,
                "error": "gh pr list failed",
            }
            return remote_branch, pull_request

        if discovered is None:
            pull_request = {
                "present": False,
                "number": None,
                "state": None,
                "merged": None,
                "merge_commit_sha": None,
                "base_ref": None,
                "url": None,
            }
            return remote_branch, pull_request

        number = discovered.get("number")
        pull_request = {
            "present": True,
            "number": number,
            "state": discovered.get("state"),
            "merged": None,
            "merge_commit_sha": None,
            "base_ref": None,
            "url": discovered.get("url"),
            "head_ref": discovered.get("headRefName"),
        }
        if number is not None:
            try:
                viewed = await self._pr.view_pr(root, int(number), timeout=timeout)
            except PullRequestError:
                pull_request["merge_read_error"] = "gh pr view failed"
                return remote_branch, pull_request
            if viewed is not None:
                pull_request["state"] = viewed.get("state", pull_request["state"])
                pull_request["merged"] = viewed.get("merged")
                merge_commit = viewed.get("mergeCommit")
                if isinstance(merge_commit, dict):
                    pull_request["merge_commit_sha"] = merge_commit.get("oid")
                pull_request["base_ref"] = viewed.get("baseRefName")
                pull_request["url"] = viewed.get("url", pull_request["url"])
        return remote_branch, pull_request

    def _audit_view(self, project_id: str) -> list[dict[str, Any]]:
        """Canonical-ancestry audit trail from the append-only ``syncs`` table.

        ``remote_url`` is deliberately omitted: it is resolved by
        ``git remote get-url origin`` and may carry embedded credentials, so a
        read-only introspection tool must not echo it.
        """
        view: list[dict[str, Any]] = []
        for row in self._sync.audit(project_id):
            view.append(
                {
                    "synced_at": row.get("synced_at"),
                    "from_head": row.get("from_head"),
                    "to_head": row.get("to_head"),
                    "branch": row.get("branch"),
                    "idempotent": row.get("idempotent"),
                }
            )
        return view

    def _decide(
        self,
        commit_valid: bool,
        provenance: dict[str, Any],
        content_check: dict[str, Any],
        canonical: dict[str, Any],
        content_equivalence: dict[str, Any],
        pull_request: dict[str, Any] | None,
    ) -> tuple[str, str, str, list[str]]:
        """Map the layered evidence to a verdict, reason, method, and warnings.

        ``method`` is one of the structured canonicalization methods
        (``exact_commit_ancestor`` / ``merged_pr_content_equivalent`` /
        ``not_merged`` / ``landing_not_in_canonical_history`` /
        ``landing_content_mismatch`` / ``insufficient_evidence``). The verdict is
        never forced into a boolean: uncertainty becomes INDETERMINATE.
        """
        warnings: list[str] = []

        if not commit_valid:
            return (
                "INDETERMINATE",
                "commit object is not present in the local repository; cannot "
                "reconcile without a git fetch (which is forbidden for read-only tools)",
                "insufficient_evidence",
                warnings,
            )
        if not provenance["job_present"]:
            return (
                "INDETERMINATE",
                "no institutional record links this commit to the project",
                "insufficient_evidence",
                warnings,
            )
        if content_check["status"] == "mismatched":
            return (
                "INDETERMINATE",
                "commit content does not match the verified candidate fingerprint",
                "insufficient_evidence",
                warnings,
            )
        status = canonical["status"]
        if status == "remote_tip_not_local":
            warnings.append(
                "canonical tip is known remotely but its object is not present "
                "locally"
            )
            return (
                "INDETERMINATE",
                "canonical default-branch tip is known remotely but not present "
                "in the local repository; ancestry cannot be verified without a "
                "git fetch (which is forbidden for read-only tools)",
                "insufficient_evidence",
                warnings,
            )
        if status in ("default_unresolved", "remote_unreachable"):
            warnings.append(
                "canonical default-branch tip could not be read from origin"
            )
            return (
                "INDETERMINATE",
                "canonical default-branch tip could not be read from origin",
                "insufficient_evidence",
                warnings,
            )
        if canonical["candidate_is_ancestor"]:
            return (
                "CANONICAL",
                "candidate commit is reachable from the origin default-branch tip",
                "exact_commit_ancestor",
                warnings,
            )

        # The candidate is NOT a direct ancestor: squash/rebase/merge-landing
        # cases. Resolve the PR and landing-ancestry signals (distinct facts).
        pr_merged = bool(pull_request and pull_request.get("merged"))
        landing_sha = canonical.get("landing_sha")
        landing_anc = canonical.get("landing_is_ancestor")
        landing_present = canonical.get("landing_present_locally")
        ce_status = content_equivalence.get("status")
        matched = canonical.get("matched_canonical_commit")

        # PR known merged but its landing SHA is not in the local object store
        # (and a fetch is forbidden): cannot verify ancestry or content.
        if pr_merged and landing_sha is not None and landing_present is False:
            warnings.append(
                "PR is merged but the landing SHA is not present locally; "
                "ancestry and content equivalence cannot be verified"
            )
            return (
                "INDETERMINATE",
                "PR is merged but the landing SHA is not present locally",
                "insufficient_evidence",
                warnings,
            )
        # PR known merged but gh reported no landing SHA (mergeCommit missing or
        # not a dict): the landing commit cannot be located, so ancestry and
        # content equivalence cannot be verified. The ``matched is None`` clause
        # defers to Case A (a local tree-identical commit already proves
        # CANONICAL without needing the landing SHA).
        if pr_merged and landing_sha is None and matched is None:
            warnings.append(
                "PR is merged but its landing SHA could not be determined"
            )
            return (
                "INDETERMINATE",
                "PR is merged but its landing SHA could not be determined",
                "insufficient_evidence",
                warnings,
            )
        # PR merged but its landing SHA is no longer in canonical history
        # (reverted / history rewritten / branch force-updated past it).
        if pr_merged and landing_anc is False:
            return (
                "NOT_CANONICAL",
                "PR was merged but its landing SHA is no longer in canonical history",
                "landing_not_in_canonical_history",
                warnings,
            )
        # Case A (squash): a tree-identical commit exists on canonical history.
        # Equal trees imply equal blobs for every changed path, so a per-path
        # mismatch is impossible here; guard against it regardless.
        if matched is not None and ce_status != "mismatch":
            return (
                "CANONICAL",
                f"content-equivalent commit ({matched}) on canonical history "
                "via squash/rebase merge",
                "merged_pr_content_equivalent",
                warnings,
            )
        # Case B (rebase / squash onto a moved base): the PR landing SHA is an
        # ancestor of the canonical tip AND the candidate's changed-path content
        # matches the landing commit's state.
        if pr_merged and landing_anc is True and ce_status == "equivalent":
            return (
                "CANONICAL",
                "PR merged, landing SHA is an ancestor of the canonical tip, and "
                "candidate content is equivalent at landing",
                "merged_pr_content_equivalent",
                warnings,
            )
        # The PR landed, but the candidate's changed-path content does not match
        # the landing state: the substantive content did not land as-is.
        if ce_status == "mismatch":
            return (
                "NOT_CANONICAL",
                "candidate content does not match the landing commit's state for "
                "the changed paths",
                "landing_content_mismatch",
                warnings,
            )
        # The landing commit is on canonical history but its per-path content
        # could not be read (a transient ls-tree failure on an already-present
        # commit): the content-equivalence check is inconclusive, so the verdict
        # must not assert "not merged" with certainty.
        if ce_status == "unavailable":
            warnings.append(
                "per-path content equivalence at landing could not be verified"
            )
            return (
                "INDETERMINATE",
                "landing commit is on canonical history but its per-path content "
                "could not be read to confirm equivalence",
                "insufficient_evidence",
                warnings,
            )
        # The PR layer (gh) was unavailable: the PR merge/landing state is
        # genuinely unknown, so "not merged" cannot be asserted with certainty.
        # Strong local evidence (candidate ancestry, a tree-identical matched
        # commit, or a content mismatch at landing) has already returned above.
        if pull_request and (
            pull_request.get("error") or pull_request.get("merge_read_error")
        ):
            warnings.append(
                "PR layer (gh) was unavailable; merge/landing state is unknown"
            )
            return (
                "INDETERMINATE",
                "PR layer (gh) was unavailable; cannot determine merge/landing state",
                "insufficient_evidence",
                warnings,
            )
        # Published but not absorbed: PR still open, PR closed without merge, or
        # no PR record at all.
        return (
            "NOT_CANONICAL",
            "commit is provenance-linked and published but not yet absorbed "
            "into canonical history",
            "not_merged",
            warnings,
        )