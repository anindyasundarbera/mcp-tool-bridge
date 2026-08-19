"""Canonical-checkout synchronization for the local development bridge.

The bridge keeps each project's guarded **canonical checkout** at
``project.root``. Development jobs branch off it as detached worktrees, and
``validate_resume`` (used by verify/commit) requires the canonical HEAD to
still equal a job's ``base_head``. When a pull request merges on remote
``main``, origin advances but the canonical checkout stays at the old commit,
so new jobs branch off a stale base. MCPB-009 adds one bounded, opt-in
capability to fast-forward the canonical checkout to a **verified remote
commit** — the read-side counterpart of MCPB-008 publication. It only ever
moves the local default branch to what ``origin/<default>`` currently points
at; it never pushes, merges, tags, deletes, or moves a remote ref.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store
from .config import Settings
from .git import GitCommandError, count_range, is_ancestor, run_git
from .jobs import DevelopmentJobManager
from .models import ProjectPolicy
from .registry import ProjectLockManager
from .verification import _redact, _remote_default_branch


class SyncError(RuntimeError):
    """Raised when the canonical checkout cannot be safely synchronized."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class SyncRecord:
    """One canonical-checkout synchronization, persisted for audit.

    Append-only: a project may be synchronized many times over its lifetime, so
    rows are keyed by an autoincrement ``sync_id`` rather than a natural key.
    Unlike jobs or publications, no single-use bound is enforced from this
    table; it exists for restart-safe auditability.
    """

    project_id: str
    synced_at: str
    from_head: str
    to_head: str
    branch: str
    remote_url: str
    idempotent: bool


def sync_to_row(record: SyncRecord) -> dict[str, Any]:
    return {
        "project_id": record.project_id,
        "synced_at": record.synced_at,
        "from_head": record.from_head,
        "to_head": record.to_head,
        "branch": record.branch,
        "remote_url": record.remote_url,
        "idempotent": 1 if record.idempotent else 0,
    }


def sync_from_row(row: dict[str, Any]) -> SyncRecord | None:
    try:
        return SyncRecord(
            project_id=row["project_id"],
            synced_at=row["synced_at"],
            from_head=row["from_head"],
            to_head=row["to_head"],
            branch=row["branch"],
            remote_url=row["remote_url"],
            idempotent=bool(row.get("idempotent", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


class SyncManager:
    """Fast-forwards a project's canonical checkout to a verified remote commit.

    The manager owns the additive ``syncs`` audit table in ``bridge.db`` and
    re-hydrates it synchronously in ``__init__`` (one append-only row per
    synchronization), exactly as the job, verification, review, and publication
    managers re-hydrate their own state.
    """

    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        jobs: DevelopmentJobManager,
        worktrees: Any | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        # Local import to avoid a hard dependency cycle in import ordering:
        # ``verification`` (imported above) already imports ``worktrees``.
        from .worktrees import GitWorktreeManager

        self.settings = settings
        self._locks = locks
        self._jobs = jobs
        self._worktrees = worktrees or GitWorktreeManager(
            settings,
            tuple(project.root for project in projects),
        )
        self._project_roots = tuple(project.root for project in projects)
        self._syncs: list[SyncRecord] = []
        self._syncs_lock = asyncio.Lock()
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_syncs()

    def _reload_syncs(self) -> None:
        """Load persisted sync-audit rows, skipping unparseable rows."""
        for row in store.load_sync_rows(self._db):
            record = sync_from_row(row)
            if record is not None:
                self._syncs.append(record)

    def _persist_sync(self, record: SyncRecord) -> None:
        store.insert_sync_row(self._db, sync_to_row(record))

    def _redaction_paths(self, project: ProjectPolicy) -> tuple[Path, ...]:
        return (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def _canonical_state(self, root: Path, *, timeout: int) -> tuple[str, str, str]:
        """Return ``(branch, head, porcelain_status)`` for the canonical checkout.

        ``branch`` is the empty string when HEAD is detached. ``porcelain_status``
        is ``git status --porcelain=v1 --untracked-files=all`` output, so a clean
        checkout (including no untracked files) yields the empty string.
        """
        branch, head, status = await asyncio.gather(
            run_git(root, "branch", "--show-current", timeout_seconds=timeout),
            run_git(root, "rev-parse", "--verify", "HEAD", timeout_seconds=timeout),
            run_git(
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                timeout_seconds=timeout,
            ),
        )
        return branch, head, status

    async def _stale_resumable_warnings(
        self,
        project: ProjectPolicy,
        target_commit: str,
    ) -> list[dict[str, Any]]:
        """Completed-but-uncommitted jobs that lose verify/commit after sync.

        These jobs retained a worktree at ``base_head`` and can still pass
        verify/commit only while the canonical HEAD equals ``base_head``. After
        sync moves HEAD to ``target_commit``, ``validate_resume`` rejects any
        whose ``base_head != target_commit``. Sync still proceeds; this is an
        informational warning, not a block.
        """
        summaries = await self._jobs.stale_resumable_job_summaries(project.id)
        warnings: list[dict[str, Any]] = []
        for summary in summaries:
            base_head = str(summary.get("base_head"))
            if base_head != target_commit:
                warnings.append(
                    {
                        "job_id": summary.get("job_id"),
                        "base_head": base_head,
                        "warning": (
                            "completed candidate was built on base_head "
                            f"{base_head[:12]}; after sync to "
                            f"{target_commit[:12]}, verify/commit will reject it "
                            "because the canonical HEAD no longer matches its base"
                        ),
                    }
                )
        return warnings

    def _response(
        self,
        project: ProjectPolicy,
        branch: str,
        from_head: str,
        to_head: str,
        remote_url: str,
        *,
        idempotent: bool,
        warnings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": "already_synchronized" if idempotent else "synchronized",
            "project_id": project.id,
            "branch": branch,
            "from_head": from_head,
            "to_head": to_head,
            "remote_url": remote_url,
            "synchronized": True,
            "idempotent": idempotent,
            "warnings": warnings,
        }

    async def sync(
        self,
        project: ProjectPolicy,
        expected_current_head: str | None = None,
        target_commit: str | None = None,
    ) -> dict[str, Any]:
        # When the caller omits the SHAs, ``sync`` auto-discovers the verified
        # remote tip after fetch (the ``prepare_canonical_for_work`` pre-work
        # pull path). When supplied, they retain their original role as an
        # optimistic-concurrency guard (``expected_current_head``) and an
        # exact-target pin (``target_commit``).
        expected_lower = (
            expected_current_head.lower() if expected_current_head else None
        )
        target_lower = target_commit.lower() if target_commit else None
        timeout = self.settings.git_command_timeout_seconds

        lock = await self._locks.for_project(project.id)
        async with lock:
            # Do not move the guarded checkout while in-flight work depends on
            # the current base. The per-project lock is held for a running job's
            # whole execution, so this primarily catches queued jobs.
            if await self._jobs.has_active_jobs(project.id):
                raise SyncError(
                    "cannot sync: project has a non-terminal development job; "
                    "wait for it to finish or cancel it, then retry"
                )

            branch, head, status = await self._canonical_state(
                project.root, timeout=timeout
            )
            if status:
                raise SyncError(
                    "canonical checkout must be clean before sync; refusing to "
                    "discard working-tree changes"
                )
            if expected_lower is not None and head != expected_lower:
                raise SyncError(
                    "canonical checkout is not at the expected current head "
                    f"{expected_lower[:12]}; re-check get_project_status "
                    "before retrying"
                )

            default_branch = await _remote_default_branch(
                project.root, timeout_seconds=timeout
            )
            if default_branch is None:
                raise SyncError(
                    "cannot determine origin default branch "
                    "(refs/remotes/origin/HEAD is unset); refuse to sync without "
                    "knowing the branch being synchronized"
                )
            if branch != default_branch:
                current = branch or "detached HEAD"
                raise SyncError(
                    f"canonical checkout must be on the default branch "
                    f"{default_branch!r} to sync; currently on {current!r}"
                )

            try:
                remote_url = await run_git(
                    project.root,
                    "remote",
                    "get-url",
                    "origin",
                    timeout_seconds=timeout,
                )
            except GitCommandError as exc:
                raise SyncError(
                    "origin remote is not configured for this project"
                ) from exc

            # Idempotent fast path for a caller-supplied, already-reached target:
            # no fetch is needed (the operator asked to be at this commit and the
            # checkout already is), so the remote-tip guard below does not apply.
            if target_lower is not None and head == target_lower:
                return await self._finish_idempotent(
                    project, branch, head, target_lower, remote_url
                )

            # Moving / auto-discovery path: fetch, then resolve the effective
            # target. This is the core safety guard — the bridge never
            # fast-forwards to an arbitrary caller-supplied commit, only to the
            # verified remote tip.
            await run_git(project.root, "fetch", "origin", timeout_seconds=timeout)
            remote_tip = await run_git(
                project.root,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{default_branch}",
                timeout_seconds=timeout,
            )
            if target_lower is not None:
                if remote_tip != target_lower:
                    raise SyncError(
                        f"target_commit {target_lower[:12]} is not the current tip of "
                        f"origin/{default_branch} (remote is at {remote_tip[:12]}); "
                        "the bridge syncs only to the verified remote default-branch tip"
                    )
                effective_target = target_lower
            else:
                # Auto-discovery (prepare_canonical_for_work): sync to whatever
                # origin's default branch currently points at.
                effective_target = remote_tip.lower()

            # Already at the (auto-discovered) remote tip: nothing to move.
            if head == effective_target:
                return await self._finish_idempotent(
                    project, branch, head, effective_target, remote_url
                )

            try:
                await run_git(
                    project.root,
                    "merge",
                    "--ff-only",
                    effective_target,
                    timeout_seconds=timeout,
                )
            except GitCommandError as exc:
                raise SyncError(
                    "fast-forward to target_commit failed; the local default branch "
                    "has diverged from origin and sync refuses to force or discard "
                    f"local history: {exc}"
                ) from exc

            new_branch, new_head, new_status = await self._canonical_state(
                project.root, timeout=timeout
            )
            if new_head != effective_target:
                raise SyncError(
                    "sync did not move the canonical checkout to target_commit"
                )
            if new_status:
                raise SyncError("canonical checkout is dirty after sync")
            if new_branch != default_branch:
                raise SyncError(
                    "canonical checkout left the default branch after sync"
                )

            warnings = await self._stale_resumable_warnings(project, effective_target)
            record = SyncRecord(
                project_id=project.id,
                synced_at=_timestamp(),
                from_head=head,
                to_head=effective_target,
                branch=new_branch,
                remote_url=remote_url,
                idempotent=False,
            )
            async with self._syncs_lock:
                self._syncs.append(record)
                self._persist_sync(record)
            return _redact(
                self._response(
                    project,
                    new_branch,
                    head,
                    effective_target,
                    remote_url,
                    idempotent=False,
                    warnings=warnings,
                ),
                self._redaction_paths(project),
            )

    async def _finish_idempotent(
        self,
        project: ProjectPolicy,
        branch: str,
        head: str,
        target: str,
        remote_url: str,
    ) -> dict[str, Any]:
        """Record and return an idempotent (no-move) sync result."""
        warnings = await self._stale_resumable_warnings(project, target)
        record = SyncRecord(
            project_id=project.id,
            synced_at=_timestamp(),
            from_head=head,
            to_head=target,
            branch=branch,
            remote_url=remote_url,
            idempotent=True,
        )
        async with self._syncs_lock:
            self._syncs.append(record)
            self._persist_sync(record)
        return _redact(
            self._response(
                project,
                branch,
                head,
                target,
                remote_url,
                idempotent=True,
                warnings=warnings,
            ),
            self._redaction_paths(project),
        )

    async def _incoming_log(
        self, root: Path, a: str, b: str, *, timeout: int
    ) -> list[str]:
        """One-line incoming commit subjects in ``a..b`` (capped at 20)."""
        out = await run_git(
            root,
            "log",
            "--oneline",
            "--no-decorate",
            "-n",
            "20",
            f"{a}..{b}",
            timeout_seconds=timeout,
        )
        return [line for line in out.splitlines() if line.strip()]

    async def _diff_stat(
        self, root: Path, a: str, b: str, *, timeout: int
    ) -> str:
        """``git diff --stat`` summary for the ``a..b`` range."""
        return await run_git(
            root, "diff", "--stat", f"{a}..{b}", timeout_seconds=timeout
        )

    async def check_for_updates(
        self, project: ProjectPolicy
    ) -> dict[str, Any]:
        """Fetch ``origin`` and classify the canonical checkout vs. the remote tip.

        The "safe-mode git pull check": fetches (updating remote-tracking refs
        only) and reports whether the canonical checkout is behind, ahead of,
        diverged from, or up to date with ``origin/<default>``, plus
        ``dirty`` / ``no_origin`` / ``fetch_failed`` states. **Never merges or
        moves the working tree** — that is ``sync``. The orchestrator calls this
        before starting new work and, on ``updates_available``, confirms a pull
        by calling ``sync`` (directly or via ``prepare_canonical_for_work`` with
        ``pull=True``).

        Read + fetch only, so it is not gated on ``has_active_jobs`` (the pull
        path keeps that guard).
        """
        timeout = self.settings.git_command_timeout_seconds
        lock = await self._locks.for_project(project.id)
        async with lock:
            branch, head, status = await self._canonical_state(
                project.root, timeout=timeout
            )
            if status:
                return _redact(
                    {
                        "state": "dirty",
                        "project_id": project.id,
                        "branch": branch,
                        "local_head": head,
                        "updates_available": False,
                        "message": (
                            "canonical checkout has uncommitted changes; clean "
                            "it before checking for updates"
                        ),
                    },
                    self._redaction_paths(project),
                )

            default_branch = await _remote_default_branch(
                project.root, timeout_seconds=timeout
            )
            if default_branch is None:
                return _redact(
                    {
                        "state": "no_origin",
                        "project_id": project.id,
                        "branch": branch,
                        "local_head": head,
                        "updates_available": False,
                        "message": (
                            "cannot determine origin default branch "
                            "(refs/remotes/origin/HEAD is unset); cannot check "
                            "for updates"
                        ),
                    },
                    self._redaction_paths(project),
                )

            try:
                await run_git(
                    project.root, "fetch", "origin", timeout_seconds=timeout
                )
            except GitCommandError as exc:
                return _redact(
                    {
                        "state": "fetch_failed",
                        "project_id": project.id,
                        "branch": branch,
                        "local_head": head,
                        "default_branch": default_branch,
                        "updates_available": False,
                        "message": f"git fetch origin failed: {exc}",
                    },
                    self._redaction_paths(project),
                )

            try:
                remote_tip = await run_git(
                    project.root,
                    "rev-parse",
                    "--verify",
                    f"refs/remotes/origin/{default_branch}",
                    timeout_seconds=timeout,
                )
            except GitCommandError as exc:
                return _redact(
                    {
                        "state": "fetch_failed",
                        "project_id": project.id,
                        "branch": branch,
                        "local_head": head,
                        "default_branch": default_branch,
                        "updates_available": False,
                        "message": (
                            f"could not resolve origin/{default_branch} tip: {exc}"
                        ),
                    },
                    self._redaction_paths(project),
                )

            head_l = head.lower()
            tip_l = remote_tip.lower()
            if head_l == tip_l:
                return _redact(
                    {
                        "state": "up_to_date",
                        "project_id": project.id,
                        "branch": branch,
                        "default_branch": default_branch,
                        "local_head": head,
                        "remote_tip": remote_tip,
                        "updates_available": False,
                    },
                    self._redaction_paths(project),
                )

            behind = await is_ancestor(
                project.root, head, remote_tip, timeout_seconds=timeout
            )
            if behind:
                behind_by = await count_range(
                    project.root, head, remote_tip, timeout_seconds=timeout
                )
                incoming = await self._incoming_log(
                    project.root, head, remote_tip, timeout=timeout
                )
                diff_stat = await self._diff_stat(
                    project.root, head, remote_tip, timeout=timeout
                )
                return _redact(
                    {
                        "state": "updates_available",
                        "project_id": project.id,
                        "branch": branch,
                        "default_branch": default_branch,
                        "local_head": head,
                        "remote_tip": remote_tip,
                        "updates_available": True,
                        "behind_by": behind_by,
                        "incoming_commits": incoming,
                        "diff_stat": diff_stat,
                        "message": (
                            f"canonical checkout is {behind_by} commit(s) behind "
                            f"origin/{default_branch}; call "
                            "prepare_canonical_for_work(pull=true) to fast-forward"
                        ),
                    },
                    self._redaction_paths(project),
                )

            ahead = await is_ancestor(
                project.root, remote_tip, head, timeout_seconds=timeout
            )
            if ahead:
                ahead_by = await count_range(
                    project.root, remote_tip, head, timeout_seconds=timeout
                )
                return _redact(
                    {
                        "state": "ahead",
                        "project_id": project.id,
                        "branch": branch,
                        "default_branch": default_branch,
                        "local_head": head,
                        "remote_tip": remote_tip,
                        "updates_available": False,
                        "ahead_by": ahead_by,
                        "message": (
                            f"canonical checkout is {ahead_by} commit(s) ahead of "
                            f"origin/{default_branch}; no updates to pull"
                        ),
                    },
                    self._redaction_paths(project),
                )

            return _redact(
                {
                    "state": "diverged",
                    "project_id": project.id,
                    "branch": branch,
                    "default_branch": default_branch,
                    "local_head": head,
                    "remote_tip": remote_tip,
                    "updates_available": False,
                    "message": (
                        "canonical checkout has diverged from "
                        f"origin/{default_branch}; fast-forward cannot reconcile — "
                        "a human steward must resolve the divergence"
                    ),
                },
                self._redaction_paths(project),
            )

    def audit(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return persisted sync records as dicts, optionally filtered by project.

        Read-only accessor for inspection; the ``syncs`` table is append-only.
        """
        return [
            {
                "project_id": record.project_id,
                "synced_at": record.synced_at,
                "from_head": record.from_head,
                "to_head": record.to_head,
                "branch": record.branch,
                "remote_url": record.remote_url,
                "idempotent": record.idempotent,
            }
            for record in self._syncs
            if project_id is None or record.project_id == project_id
        ]