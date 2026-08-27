from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .git import GitCommandError, count_range, is_ancestor, run_git
from .models import ProjectPolicy


class WorktreeError(RuntimeError):
    """Raised when an isolated job worktree cannot be created or trusted."""


@dataclass(frozen=True, slots=True)
class JobWorktree:
    path: Path
    base_head: str
    # M-CC-07: recovered completed worktrees are exposed read-only. The flag is
    # additive (defaults False) so every existing construction is untouched; a
    # read-only worktree is a detached checkout of an immutable committed
    # candidate and must never be used as a resume/verify target.
    read_only: bool = False


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

    async def _remote_default_tip_local(
        self, project: ProjectPolicy
    ) -> tuple[str, str] | None:
        """Origin default-branch ``(branch, tip)`` from local remote-tracking refs only.

        No ``git fetch``: reads ``refs/remotes/origin/HEAD`` and
        ``refs/remotes/origin/<branch>`` as they currently stand in the local
        refs. Returns ``None`` when the symref/tip is unset (no origin, or origin
        never fetched). The staleness guard uses this so a default worktree
        creation never blocks on the network — freshness of the tracking ref is
        established by ``prepare_canonical_for_work`` (which fetches) before work
        starts, per the orchestrator pre-work convention.
        """
        timeout = self.settings.git_command_timeout_seconds
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

    async def _refuse_if_stale_base(
        self, project: ProjectPolicy, local_head: str
    ) -> None:
        """Raise ``WorktreeError`` if the canonical checkout is behind origin's default.

        Uses **local remote-tracking refs only** (no fetch), so this is a cheap
        backstop on the worktree-creation hot path: it refuses when the tracking
        ref already shows the canonical HEAD is behind ``origin/<default>``. The
        tracking ref is made fresh by ``prepare_canonical_for_work`` (which
        fetches) before work starts; if no one has fetched, the ref is unset or
        stale and the guard no-ops rather than blocking on the network. Only the
        clean ``behind`` case is actionable — a diverged or ahead checkout is
        left to the existing baseline checks.
        """
        remote = await self._remote_default_tip_local(project)
        if remote is None:
            return
        branch, remote_tip = remote
        if remote_tip.lower() == local_head.lower():
            return
        timeout = self.settings.git_command_timeout_seconds
        behind = await is_ancestor(
            project.root, local_head, remote_tip, timeout_seconds=timeout
        )
        if not behind:
            return
        behind_by = await count_range(
            project.root, local_head, remote_tip, timeout_seconds=timeout
        )
        raise WorktreeError(
            f"canonical checkout is {behind_by} commit(s) behind "
            f"origin/{branch}; call prepare_canonical_for_work(pull=true) to "
            "fast-forward to the verified remote tip, then retry "
            "send_to_local_dev"
        )

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

        # Pre-work staleness guard (MCPB-009 companion): when the caller did not
        # pin a base, the worktree branches off the local canonical HEAD. If a
        # pull request has since merged on the remote, that HEAD is stale and the
        # new work would be built on out-of-date code. Refuse with an actionable
        # message so the orchestrator can self-serve a fast-forward via
        # prepare_canonical_for_work(pull=true) instead of hitting an opaque
        # downstream error. Skipped silently when there is no origin (offline /
        # local-only repos) and when the caller pinned requested_base.
        if requested_base is None:
            await self._refuse_if_stale_base(project, local_head)

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

    async def create_at_commit(
        self,
        project: ProjectPolicy,
        job_id: str,
        base_head: str,
    ) -> JobWorktree:
        """Create a detached worktree pinned to an exact local commit (M-CC-03).

        Unlike ``create`` (which pins to the local canonical HEAD or the fetched
        ``origin/<default>`` tip) and ``create_detached_at_head`` (which requires
        the canonical checkout itself to be AT ``base_head``), this pins the
        worktree to an arbitrary commit that already exists in the shared object
        store — e.g. a completed job's published ``committed_sha``, so a
        successor job branches off the immutable completed candidate rather than
        the canonical HEAD. Linked worktrees share the canonical repository's
        object database, so a commit created in an earlier job worktree is
        resolvable here.

        The canonical checkout must be clean (same invariant as ``create``): the
        successor is a fresh development job, so a dirty source checkout fails
        closed before any worktree is created. ``base_head`` must be a 40-hex
        commit that resolves in the object store; anything else (a short SHA, a
        branch name, a missing commit) is rejected so the worktree can never be
        pinned to an ambiguous or fabricated base.
        """
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

        # Resolve the exact commit: reject short SHAs, branch names, and commits
        # absent from the shared object store (fail closed, never guess).
        try:
            resolved_head = await run_git(
                project.root,
                "rev-parse",
                "--verify",
                f"{base_head}^{{commit}}",
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
        except GitCommandError as exc:
            raise WorktreeError(
                f"base commit {base_head[:12]} is not a resolvable commit in "
                "the local object store"
            ) from exc
        pin = resolved_head.strip().lower()

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
                pin,
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
            if worktree_head.lower() != pin or worktree_status:
                raise WorktreeError("created job worktree has an invalid baseline")
            completed = True
            return JobWorktree(path=resolved, base_head=pin)
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

    async def create_detached_at_head(
        self,
        project: ProjectPolicy,
        job_id: str,
        base_head: str,
    ) -> JobWorktree:
        """Create a detached worktree at an exact ``base_head`` (MCPB-012).

        Unlike ``create``, this does NOT require the canonical checkout to be
        clean: the whole purpose of working-tree candidate capture is to read a
        dirty checkout. Only read-only git is run against the source checkout
        (``rev-parse`` and ``worktree add --detach``); the source working tree,
        index, refs, and configuration are never staged, reset, restored,
        checked out, cleaned, or written. The new worktree is pinned to
        ``base_head`` (the exact canonical HEAD the caller bound the capture to)
        and must come up clean at that commit.
        """
        root = self._root_for(project)
        project_directory = _ensure_private_directory(root / project.id)

        path = project_directory / job_id
        if path.exists() or path.is_symlink():
            raise WorktreeError("job worktree path already exists")

        timeout = self.settings.git_command_timeout_seconds
        local_head = await run_git(
            project.root,
            "rev-parse",
            "--verify",
            "HEAD",
            timeout_seconds=timeout,
        )
        if local_head.lower() != base_head.lower():
            raise WorktreeError(
                f"canonical HEAD {local_head[:12]} does not match the bound "
                f"base_head {base_head[:12]}"
            )

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
                timeout_seconds=timeout,
            )
            created = True

            after_head = await run_git(
                project.root,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=timeout,
            )
            if after_head.lower() != local_head.lower():
                raise WorktreeError(
                    "canonical HEAD moved during worktree creation"
                )

            resolved = path.resolve(strict=True)
            if resolved.parent != project_directory.resolve(strict=True):
                raise WorktreeError("job worktree escaped the configured worktree root")
            worktree_root = await run_git(
                resolved,
                "rev-parse",
                "--show-toplevel",
                timeout_seconds=timeout,
            )
            worktree_head = await run_git(
                resolved,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=timeout,
            )
            worktree_status = await run_git(
                resolved,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                timeout_seconds=timeout,
            )
            if Path(worktree_root).resolve(strict=True) != resolved:
                raise WorktreeError("created job path is not a Git worktree root")
            if worktree_head.lower() != base_head.lower() or worktree_status:
                raise WorktreeError("created job worktree has an invalid baseline")
            completed = True
            return JobWorktree(path=resolved, base_head=base_head.lower())
        except (GitCommandError, OSError) as exc:
            raise WorktreeError(str(exc)) from exc
        finally:
            if created and not completed:
                try:
                    await run_git(
                        project.root,
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(path),
                        timeout_seconds=timeout,
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

    async def recover_completed_worktree(
        self,
        project: ProjectPolicy,
        job_id: str,
        committed_sha: str,
        base_head: str,
        paths: tuple[str, ...],
        expected_fingerprint: str,
    ) -> JobWorktree:
        """Re-attach a vanished completed worktree read-only (M-CC-07).

        Recreates the completed job's isolated worktree as a detached checkout
        of the bridge-created ``committed_sha`` (the immutable committed
        candidate), then re-derives the candidate fingerprint from the committed
        object and proves it matches the verified ``expected_fingerprint``. The
        recreated worktree is exposed READ-ONLY: no branch is created, no refs
        are mutated, and the canonical checkout is never written. The only
        mutations are git worktree administrative entries — ``worktree prune``
        clears the stale registration left behind by the vanished directory so
        ``worktree add --detach`` can register the new checkout at the original
        job path. The returned ``JobWorktree`` carries ``read_only=True``.

        ``base_head`` and ``expected_fingerprint`` must be the exact values
        recorded at verification time (the verification record's ``base_head``
        and ``diff_fingerprint``), and ``paths`` the verified ordered path list;
        a mismatch fails closed rather than exposing unverified content.
        """
        # Lazy import breaks the verification<->worktrees module cycle
        # (verification imports worktrees at module load). Done at the top of
        # the method so the names are bound before any except clause runs.
        from .verification import VerificationError, committed_state

        root = self._root_for(project)
        project_directory = _ensure_private_directory(root / project.id)
        path = project_directory / job_id
        if path.exists() or path.is_symlink():
            raise WorktreeError("job worktree path already exists")
        if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
            raise WorktreeError("invalid job_id")
        target = committed_sha.lower()
        if len(target) != 40 or any(
            character not in "0123456789abcdef" for character in target
        ):
            raise WorktreeError("committed_sha must be a 40-character hex SHA-1")
        if not paths:
            raise WorktreeError("cannot recover a candidate with no changed paths")

        timeout = self.settings.git_command_timeout_seconds
        canonical_head = await run_git(
            project.root,
            "rev-parse",
            "--verify",
            "HEAD",
            timeout_seconds=timeout,
        )
        created = False
        completed = False
        try:
            # Drop the stale worktree registration whose working tree vanished
            # so ``worktree add`` can re-attach at the original job path. Only
            # registrations whose worktree directory is missing are pruned;
            # live worktrees, refs, and the canonical checkout are untouched.
            await run_git(
                project.root,
                "worktree",
                "prune",
                timeout_seconds=timeout,
            )
            await run_git(
                project.root,
                "worktree",
                "add",
                "--detach",
                "--",
                str(path),
                target,
                timeout_seconds=timeout,
            )
            created = True

            resolved = path.resolve(strict=True)
            if resolved.parent != project_directory.resolve(strict=True):
                raise WorktreeError("job worktree escaped the configured worktree root")
            worktree_root = await run_git(
                resolved,
                "rev-parse",
                "--show-toplevel",
                timeout_seconds=timeout,
            )
            worktree_head = await run_git(
                resolved,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=timeout,
            )
            worktree_status = await run_git(
                resolved,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                timeout_seconds=timeout,
            )
            branch = await run_git(
                resolved,
                "branch",
                "--show-current",
                timeout_seconds=timeout,
            )
            after_canonical_head = await run_git(
                project.root,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=timeout,
            )
            if Path(worktree_root).resolve(strict=True) != resolved:
                raise WorktreeError("created job path is not a Git worktree root")
            if worktree_head.lower() != target:
                raise WorktreeError(
                    "recovered worktree HEAD does not match the committed candidate"
                )
            if worktree_status or branch:
                raise WorktreeError(
                    "recovered worktree is not a clean detached checkout"
                )
            if after_canonical_head.lower() != canonical_head.lower():
                raise WorktreeError("canonical HEAD moved during recovery")

            # Re-derive the candidate fingerprint from the committed object and
            # compare it against the fingerprint recorded at verification time.
            recovered_state = await committed_state(
                self.settings,
                project,
                JobWorktree(path=resolved, base_head=base_head.lower()),
                target,
                base_head.lower(),
                paths,
            )
            if recovered_state.fingerprint.lower() != expected_fingerprint.lower():
                raise WorktreeError(
                    "recovered worktree fingerprint does not match the verified candidate"
                )
            completed = True
            return JobWorktree(
                path=resolved,
                base_head=base_head.lower(),
                read_only=True,
            )
        except (GitCommandError, VerificationError, OSError) as exc:
            raise WorktreeError(str(exc)) from exc
        finally:
            if created and not completed:
                # Git may have registered a partially-created worktree. A forced
                # removal is safe here because this path was allocated by this call
                # and has not been handed to any caller.
                try:
                    await run_git(
                        project.root,
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(path),
                        timeout_seconds=timeout,
                    )
                except (GitCommandError, OSError):
                    pass

    async def reset_worktree_to_base(
        self,
        project: ProjectPolicy,
        worktree: JobWorktree,
    ) -> None:
        """Reset a job worktree's working tree to the clean base state (M-CC-10).

        Candidate-corruption rollback: on a re-derive failure the worktree is
        returned to the exact clean detached state it had at ``base_head``.
        The index is cleared and every tracked file restored to ``HEAD``
        (``git reset --hard``), then every untracked file/directory is removed
        (``git clean -fd``). Because the worktree is isolated and every file in
        it is a candidate artifact, this is the faithful recovery the content-
        addressed checkpoint ledger describes (the ledger retains paths + a
        digest, never file bytes). Only the isolated worktree is touched — the
        canonical checkout is never read or written. The path is re-validated
        as a trusted directory inside the project's worktree root before any
        destructive git runs, so a swapped/symlinked path is refused.
        """
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

        timeout = self.settings.git_command_timeout_seconds
        await run_git(
            resolved,
            "reset",
            "--hard",
            "HEAD",
            timeout_seconds=timeout,
        )
        await run_git(
            resolved,
            "clean",
            "-fd",
            "--",
            ".",
            timeout_seconds=timeout,
        )
        status = await run_git(
            resolved,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            timeout_seconds=timeout,
        )
        if status:
            raise WorktreeError("job worktree did not return to a clean base state")

    async def recreate_detached(
        self,
        project: ProjectPolicy,
        job_id: str,
        base_head: str,
    ) -> JobWorktree:
        """Recreate a lost job worktree detached at an exact ``base_head``.

        M-CC-10: reconstructs a job worktree whose directory vanished. Unlike
        ``create_detached_at_head`` (MCPB-012) this does NOT require the
        canonical HEAD to equal ``base_head`` — reconstruction must work after
        the canonical checkout has moved on. Only the ``base_head`` object
        resolving in the canonical repository is required (read-only
        ``cat-file``); the canonical working tree, index, and refs are never
        written. The new worktree comes up clean at ``base_head`` and its path
        is validated as a trusted directory inside the project's worktree root.
        """
        root = self._root_for(project)
        project_directory = _ensure_private_directory(root / project.id)

        path = project_directory / job_id
        if path.exists() or path.is_symlink():
            raise WorktreeError("job worktree path already exists")

        timeout = self.settings.git_command_timeout_seconds
        # Read-only: confirm the base commit object exists in the canonical
        # repository before creating any worktree from it.
        await run_git(
            project.root,
            "cat-file",
            "-e",
            f"{base_head}^{{commit}}",
            timeout_seconds=timeout,
        )

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
                timeout_seconds=timeout,
            )
            created = True

            resolved = path.resolve(strict=True)
            if resolved.parent != project_directory.resolve(strict=True):
                raise WorktreeError("job worktree escaped the configured worktree root")
            worktree_root = await run_git(
                resolved,
                "rev-parse",
                "--show-toplevel",
                timeout_seconds=timeout,
            )
            worktree_head = await run_git(
                resolved,
                "rev-parse",
                "--verify",
                "HEAD",
                timeout_seconds=timeout,
            )
            worktree_status = await run_git(
                resolved,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                timeout_seconds=timeout,
            )
            if Path(worktree_root).resolve(strict=True) != resolved:
                raise WorktreeError("created job path is not a Git worktree root")
            if worktree_head.lower() != base_head.lower() or worktree_status:
                raise WorktreeError("recreated job worktree has an invalid baseline")
            completed = True
            return JobWorktree(path=resolved, base_head=base_head.lower())
        except (GitCommandError, OSError) as exc:
            raise WorktreeError(str(exc)) from exc
        finally:
            if created and not completed:
                # Git may have registered a partially-created worktree. A forced
                # removal is safe here because this path was allocated by this call
                # and has not been handed to any caller.
                try:
                    await run_git(
                        project.root,
                        "worktree",
                        "remove",
                        "--force",
                        "--",
                        str(path),
                        timeout_seconds=timeout,
                    )
                except (GitCommandError, OSError):
                    pass

    async def remove(
        self,
        project: ProjectPolicy,
        worktree: JobWorktree,
    ) -> None:
        """M-CC-08: permanently remove a job worktree (discard_candidate).

        The path must still be inside this project's configured worktree root
        and must not be a symlink (a symlink or an escaped path is refused, not
        followed). The removal is ``git worktree remove --force -- <path>`` from
        the project root — a fixed argument vector, no shell. Already-removed
        worktrees are a no-op success so the control is repeat-safe.
        """
        root = self._root_for(project)
        expected_parent = (root / project.id).resolve(strict=True)
        try:
            metadata = os.lstat(worktree.path)
        except FileNotFoundError:
            # Already removed (or never created): idempotent success.
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise WorktreeError("job worktree is not a trusted directory")
        resolved = worktree.path.resolve(strict=True)
        if resolved.parent != expected_parent:
            raise WorktreeError("job worktree escaped the configured worktree root")
        try:
            await run_git(
                project.root,
                "worktree",
                "remove",
                "--force",
                "--",
                str(resolved),
                timeout_seconds=self.settings.git_command_timeout_seconds,
            )
        except (GitCommandError, OSError) as exc:
            raise WorktreeError(str(exc)) from exc
        if resolved.exists() or resolved.is_symlink():
            raise WorktreeError("git worktree remove left the candidate in place")
