from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .git import GitCommandError, run_git
from .models import ProjectPolicy


class WorktreeError(RuntimeError):
    """Raised when an isolated job worktree cannot be created or trusted."""


@dataclass(frozen=True, slots=True)
class JobWorktree:
    path: Path
    base_head: str


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _ensure_private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise WorktreeError("worktree root must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    nofollow_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            try:
                metadata = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                metadata = os.stat(
                    component,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            if stat.S_ISLNK(metadata.st_mode):
                raise WorktreeError("worktree root must not contain symbolic links")
            if not stat.S_ISDIR(metadata.st_mode):
                raise WorktreeError("worktree root components must be directories")
            next_descriptor = os.open(
                component,
                nofollow_flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            if index == len(components) - 1:
                final_mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
                if final_mode & 0o077:
                    raise WorktreeError(
                        "worktree root must not be accessible by group or other users"
                    )
        return path.resolve(strict=True)
    except OSError as exc:
        raise WorktreeError(f"could not create trusted worktree root: {exc}") from exc
    finally:
        os.close(descriptor)


class GitWorktreeManager:
    def __init__(
        self,
        settings: Settings,
        protected_project_roots: tuple[Path, ...] = (),
    ):
        self.settings = settings
        self._protected_project_roots = protected_project_roots

    def ensure_state_root(self) -> Path:
        """Create and validate the private ``.state`` directory.

        ``.state`` lives beneath ``job_worktree_root`` and holds the durable
        job/verification database. It must stay outside every configured project
        root, exactly like the worktree root itself, and be a private directory
        not accessible by group or other users. Validated eagerly at manager
        construction so a misconfiguration is caught before the first job runs.
        """
        state_root = self.settings.job_worktree_root / ".state"
        protected_roots = {
            root.resolve(strict=False) for root in self._protected_project_roots
        }
        prospective = state_root.resolve(strict=False)
        for protected_root in protected_roots:
            if _is_within(prospective, protected_root) or _is_within(
                protected_root, prospective
            ):
                raise WorktreeError(
                    "JOB_WORKTREE_ROOT must be outside every configured project root"
                )
        return _ensure_private_directory(state_root)

    def _root_for(self, project: ProjectPolicy) -> Path:
        configured = self.settings.job_worktree_root
        project_root = project.root.resolve(strict=True)
        protected_roots = {
            root.resolve(strict=True) for root in self._protected_project_roots
        }
        protected_roots.add(project_root)
        prospective = configured.resolve(strict=False)
        for protected_root in protected_roots:
            if _is_within(prospective, protected_root) or _is_within(
                protected_root, prospective
            ):
                raise WorktreeError(
                    "JOB_WORKTREE_ROOT must be outside every configured project root"
                )
        root = _ensure_private_directory(configured)
        for protected_root in protected_roots:
            if _is_within(root, protected_root) or _is_within(protected_root, root):
                raise WorktreeError(
                    "JOB_WORKTREE_ROOT must be outside every configured project root"
                )
        return root

    async def _source_state(self, project: ProjectPolicy) -> tuple[str, str]:
        timeout = self.settings.git_command_timeout_seconds
        status = await run_git(
            project.root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            timeout_seconds=timeout,
        )
        head = await run_git(
            project.root,
            "rev-parse",
            "--verify",
            "HEAD",
            timeout_seconds=timeout,
        )
        return status, head

    async def _resolve_remote_default_tip(
        self, project: ProjectPolicy
    ) -> tuple[str, str] | None:
        """Fetch ``origin`` and return ``(branch, tip_sha)`` of its default branch.

        Returns ``None`` when the project has no ``origin`` remote or no
        resolvable ``refs/remotes/origin/HEAD`` (offline / local-only repos).
        ``branch`` is the default branch name, ``tip_sha`` the current remote
        tip. Fetch is opt-in per job via ``requested_base``; the bridge never
        fetches silently for projects that did not ask for a pinned base.
        """
        timeout = self.settings.git_command_timeout_seconds
        try:
            await run_git(
                project.root,
                "fetch",
                "origin",
                timeout_seconds=timeout,
            )
        except GitCommandError:
            return None
        try:
            full = await run_git(
                project.root,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                timeout_seconds=timeout,
            )
        except GitCommandError:
            return None
        prefix = "refs/remotes/origin/"
        if not full.startswith(prefix):
            return None
        branch = full[len(prefix):]
        try:
            tip = await run_git(
                project.root,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{branch}",
                timeout_seconds=timeout,
            )
        except GitCommandError:
            return None
        return branch, tip

    async def create(
        self,
        project: ProjectPolicy,
        job_id: str,
        requested_base: str | None = None,
    ) -> JobWorktree:
        root = self._root_for(project)
        project_directory = _ensure_private_directory(root / project.id)

        path = project_directory / job_id
        if path.exists() or path.is_symlink():
            raise WorktreeError("job worktree path already exists")

        before_status, local_head = await self._source_state(project)
        if before_status:
            raise WorktreeError(
                "configured project checkout must be clean before starting a job"
            )
        base_head = local_head

        # M-CT-01: when a caller requests a pinned base, fetch ``origin`` and
        # pin the worktree to the fetched remote default-branch tip rather than
        # branching off the local canonical HEAD (which may be stale). The
        # local checkout is left untouched; only the worktree uses the pin.
        if requested_base is not None:
            remote = await self._resolve_remote_default_tip(project)
            if remote is None:
                raise WorktreeError(
                    "requested_base requires a resolvable origin default branch; "
                    "this project has no origin remote or refs/remotes/origin/HEAD"
                )
            branch, remote_tip = remote
            expected = requested_base.lower()
            if remote_tip.lower() != expected:
                raise WorktreeError(
                    f"requested_base {expected[:12]} is not the current tip of "
                    f"origin/{branch} (remote is at {remote_tip[:12]})"
                )
            base_head = expected

        created = False
        completed = False
        try:
            await run_git(
                project.root,
                "worktree",
                "add",
                "--detach",
                "--",
                str(path),
                base_head,
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
            created = True

            after_status, after_head = await self._source_state(project)
            if after_status or after_head != local_head:
                raise WorktreeError(
                    "configured project checkout changed during worktree creation"
                )

            resolved = path.resolve(strict=True)
            if resolved.parent != project_directory.resolve(strict=True):
                raise WorktreeError("job worktree escaped the configured worktree root")
            worktree_root = await run_git(
                resolved,
                "rev-parse",
                "--show-toplevel",
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
            worktree_head = await run_git(
                resolved,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
            worktree_status = await run_git(
                resolved,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
            if Path(worktree_root).resolve(strict=True) != resolved:
                raise WorktreeError("created job path is not a Git worktree root")
            if worktree_head != base_head or worktree_status:
                raise WorktreeError("created job worktree has an invalid baseline")
            completed = True
            return JobWorktree(path=resolved, base_head=base_head)
        except (GitCommandError, OSError) as exc:
            raise WorktreeError(str(exc)) from exc
        finally:
            if created and not completed:
                # Git may have registered a partially-created worktree. A forced
                # removal is safe here because this path was allocated by this call
                # and has not been handed to Claude.
                try:
                    await run_git(
                        project.root,
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(path),
                        timeout_seconds=self.settings.git_command_timeout_seconds,
                    )
                except (GitCommandError, OSError):
                    pass

    async def validate_resume(
        self,
        project: ProjectPolicy,
        worktree: JobWorktree,
    ) -> None:
        status, head = await self._source_state(project)
        if status:
            raise WorktreeError(
                "configured project checkout must be clean before resuming a job"
            )
        if head != worktree.base_head:
            raise WorktreeError("configured project HEAD moved since the job started")

        root = self._root_for(project)
        expected_parent = (root / project.id).resolve(strict=True)
        try:
            metadata = os.lstat(worktree.path)
        except FileNotFoundError as exc:
            raise WorktreeError("job worktree no longer exists") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorktreeError("job worktree is not a trusted directory")
        resolved = worktree.path.resolve(strict=True)
        if resolved.parent != expected_parent:
            raise WorktreeError("job worktree escaped the configured worktree root")

        actual_root = await run_git(
            resolved,
            "rev-parse",
            "--show-toplevel",
            timeout_seconds=self.settings.git_command_timeout_seconds,
        )
        actual_head = await run_git(
            resolved,
            "rev-parse",
            "--verify",
            "HEAD",
            timeout_seconds=self.settings.git_command_timeout_seconds,
        )
        if Path(actual_root).resolve(strict=True) != resolved:
            raise WorktreeError("job path is no longer a Git worktree root")
        if actual_head != worktree.base_head:
            raise WorktreeError("job worktree HEAD moved since the job started")
