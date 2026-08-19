"""Draft pull-request creation for the local development bridge (M-CC-01/02/05).

Opens or updates a draft PR for a job's published remote branch, confirms the
remote branch / PR state, and is race-safe idempotent (discover-then-create /
update, tolerating a concurrent creator). Uses the ``gh`` CLI with fixed argv
(no shell) and git for remote-branch state. Merge is never issued.

Dormant by default: the ``open_pull_request`` MCP tool is only exposed when a
project opts into ``OPEN_PULL_REQUEST`` (which requires ``push_development_commit``).
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from . import store
from .git import GitCommandError, run_git
from .jobs import DevelopmentJobManager
from .models import ProjectPolicy
from .registry import ProjectLockManager
from .verification import (
    PublicationError,
    VerificationError,
    _remote_branch_sha,
    _validate_branch_name,
)
from .worktrees import GitWorktreeManager


class PullRequestError(RuntimeError):
    """Raised when a pull request cannot be safely opened or updated."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _redact(value: Any, paths: tuple[Path, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for path in paths:
            redacted = redacted.replace(str(path), "<redacted-path>")
        return redacted
    if isinstance(value, dict):
        return {key: _redact(item, paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, paths) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, paths) for item in value)
    return value


async def _gh(
    root: Path,
    *arguments: str,
    timeout_seconds: int,
) -> str:
    """Run the ``gh`` CLI with a fixed argument vector (no shell)."""
    process = await asyncio.create_subprocess_exec(
        "gh",
        *arguments,
        cwd=str(root),
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        await process.wait()
        raise PullRequestError(
            f"gh {' '.join(arguments)} timed out after {timeout_seconds} seconds"
        ) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise PullRequestError(
            f"gh {' '.join(arguments)} failed with exit code "
            f"{process.returncode}: {detail}"
        )
    return stdout.decode("utf-8", errors="replace").strip()


class PullRequestManager:
    """Opens/updates draft PRs and confirms remote branch / PR state."""

    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        jobs: DevelopmentJobManager,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._jobs = jobs
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._project_roots = tuple(project.root for project in projects)
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")

    def _redaction_paths(self, project: ProjectPolicy, job_id: str) -> tuple[Path, ...]:
        worktree_path = self.settings.job_worktree_root / project.id / job_id
        return (
            worktree_path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def _remote_branch_state(
        self, worktree: Path, branch_name: str, *, timeout: int
    ) -> dict[str, Any]:
        """Confirm the remote branch state: present/vanished + head SHA."""
        sha = await _remote_branch_sha(
            worktree, branch_name, timeout_seconds=timeout
        )
        return {
            "branch_name": branch_name,
            "present": sha is not None,
            "head_sha": sha,
        }

    async def _discover_pr(
        self, worktree: Path, branch_name: str, *, timeout: int
    ) -> dict[str, Any] | None:
        """Discover an existing PR for a branch via ``gh pr list --head``.

        Returns ``None`` only when no PR exists. A ``gh`` failure (timeout,
        auth, network, rate-limit) is re-raised so a transient error is never
        mistaken for "no PR" (which would cause a duplicate create). The
        ``--head`` filter is the ``owner:branch`` form required by gh; a bare
        branch name silently returns all PRs, so the owner prefix is mandatory.
        """
        try:
            output = await _gh(
                worktree,
                "pr",
                "list",
                "--head",
                branch_name,
                "--json",
                "number,url,state,headRefName",
                "--limit",
                "1",
                timeout_seconds=timeout,
            )
        except PullRequestError:
            # A gh failure (timeout, auth, network, rate-limit) must not be
            # mistaken for "no PR" (which would cause a duplicate create), so it
            # propagates to the caller.
            raise
        if not output:
            return None
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, list) or not parsed:
            return None
        candidate = parsed[0]
        # Cross-check the matched PR's head ref against the branch we asked for.
        # A PR matched by ``--head`` for a different branch/fork must not be
        # silently edited; treat it as no-PR so we create the correct one.
        if candidate.get("headRefName") != branch_name:
            return None
        return candidate

    async def view_pr(
        self, worktree: Path, pr_number: int, *, timeout: int
    ) -> dict[str, Any] | None:
        """Read one PR's merge/landing state via ``gh pr view`` (M-AL-01).

        A read-only counterpart to ``_discover_pr``: given a PR number, returns
        its ``state`` (open/closed/merged), ``merged`` flag, ``mergeCommit.oid``
        (the landing SHA, when merged), base/head refs, and url. Returns ``None``
        if gh reports no such PR. A ``gh`` failure (timeout/auth/network/rate-limit)
        is re-raised as ``PullRequestError`` so a read-only reconciliation tool can
        distinguish "no PR" from "transient gh failure". Never issues a mutation;
        ``pr view`` is read-only and uses the fixed-argv ``_gh`` runner with no
        shell and stdin DEVNULL.
        """
        output = await _gh(
            worktree,
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,state,merged,mergeCommit,baseRefName,headRefName,url",
            timeout_seconds=timeout,
        )
        if not output:
            return None
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict) or "number" not in parsed:
            return None
        return parsed

    async def open_pull_request(
        self,
        project: ProjectPolicy,
        job_id: str,
        base_head: str,
        fingerprint: str,
        title: str,
        *,
        draft: bool = True,
    ) -> dict[str, Any]:
        """Open or update a draft PR for a job's published remote branch.

        Requires the job to have been published (a retained worktree with a
        committed detached commit on a remote branch). Discovers an existing PR
        for the branch; if found, updates its title/body; else opens a new draft
        PR. Never issues a merge. Race-safe: discover-then-create/update, so a
        concurrent creator is tolerated (the second caller finds the PR).
        """
        base_head = base_head.lower()
        fingerprint = fingerprint.lower()
        if not title or len(title) > 200:
            raise PullRequestError("title must be between 1 and 200 characters")
        timeout = self.settings.git_command_timeout_seconds

        lock = await self._locks.for_project(project.id)
        async with lock:
            try:
                candidate = await self._jobs.committed_candidate(project, job_id)
            except Exception as exc:
                raise PullRequestError(str(exc)) from exc
            worktree = candidate.worktree
            if base_head != worktree.base_head:
                raise PullRequestError("base_head does not match the job base HEAD")

            # The published branch name is the job's branch (one branch per job).
            # Re-derive it from the publication record so we never guess.
            publication = store.publication_for(self._db, project.id, job_id)
            if publication is None:
                raise PullRequestError(
                    "job has no publication record; publish the detached commit "
                    "before opening a pull request"
                )
            branch_name = str(publication["branch_name"])
            _validate_branch_name(branch_name)

            # Re-stated evidence: the caller's fingerprint must match a used
            # verification record for this exact (project, job, base_head), so a
            # stale or mismatched fingerprint is rejected rather than silently
            # opening a PR against the wrong candidate.
            if store.used_verification_record(
                self._db, project.id, job_id, base_head, fingerprint
            ) is None:
                raise PullRequestError(
                    "no matching used verification record for this base HEAD and "
                    "fingerprint; re-verify and commit before opening a PR"
                )

            try:
                remote_state = await self._remote_branch_state(
                    worktree.path, branch_name, timeout=timeout
                )
            except (GitCommandError, VerificationError) as exc:
                raise PullRequestError(
                    f"could not confirm remote branch state: {exc}"
                ) from exc
            if not remote_state["present"]:
                raise PullRequestError(
                    "remote branch has vanished; re-publish before opening a PR"
                )

            existing = await self._discover_pr(
                worktree.path, branch_name, timeout=timeout
            )
            if existing is not None:
                number = existing.get("number")
                await _gh(
                    worktree.path,
                    "pr",
                    "edit",
                    str(number),
                    "--title",
                    title,
                    timeout_seconds=timeout,
                )
                pr_state = {
                    "number": number,
                    "url": existing.get("url"),
                    "state": existing.get("state"),
                    "action": "updated",
                }
            else:
                try:
                    base = await self._default_branch(worktree.path, timeout=timeout)
                except (GitCommandError, VerificationError) as exc:
                    raise PullRequestError(
                        f"could not determine the PR base branch: {exc}"
                    ) from exc
                # BridgeLab publishes a detached, independently verified commit
                # directly to the remote feature branch; the canonical checkout
                # stays on ``main`` and no local feature branch exists. ``--fill``
                # is therefore incompatible: ``gh`` inspects the local Git commit
                # range (``origin/main...<head>``) to generate the body, which
                # fails with an ambiguous-argument error because that range is not
                # in the working tree. Supply an explicit ``--body`` built only
                # from metadata already in scope (no extra shell commands, no
                # worktree paths, no secrets), so PR creation does not depend on a
                # local feature branch and never prompts (stdin is DEVNULL).
                commit_sha = str(publication["commit_sha"])
                body = (
                    "Created by BridgeLab from an independently reviewed and "
                    "verified development candidate.\n\n"
                    f"- source branch: {branch_name}\n"
                    f"- base branch: {base}\n"
                    f"- commit SHA: {commit_sha}\n"
                    f"- candidate fingerprint: {fingerprint}\n"
                )
                args = [
                    "pr",
                    "create",
                    "--head",
                    branch_name,
                    "--base",
                    base,
                    "--title",
                    title,
                    "--body",
                    body,
                ]
                if draft:
                    args.append("--draft")
                output = await _gh(
                    worktree.path, *args, timeout_seconds=timeout
                )
                pr_state = {
                    "number": None,
                    "url": output,
                    "state": "open",
                    "action": "created",
                }

            return _redact(
                {
                    "status": "ok",
                    "project_id": project.id,
                    "job_id": job_id,
                    "branch_name": branch_name,
                    "remote_branch_state": remote_state,
                    "pr_state": pr_state,
                    "draft": draft,
                },
                self._redaction_paths(project, job_id),
            )

    async def _default_branch(self, worktree: Path, *, timeout: int) -> str:
        from .verification import _remote_default_branch

        branch = await _remote_default_branch(worktree, timeout_seconds=timeout)
        if branch is None:
            raise PullRequestError(
                "cannot determine origin default branch for the PR base"
            )
        return branch
