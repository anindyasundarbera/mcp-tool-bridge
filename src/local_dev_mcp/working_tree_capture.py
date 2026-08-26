"""Working-tree candidate capture (MCPB-012).

Captures an approved set of uncommitted working-tree changes from a *dirty*
canonical checkout into a retained BridgeLab candidate **without modifying the
canonical checkout**. The capture is read-only against the source: it never
stages, resets, restores, checks out, cleans, renames, deletes, or writes any
canonical working-tree file, and it never alters index state, Git configuration,
remotes, branches, or ``.reasonix`` content.

The captured candidate is a normal retained governed candidate — a development
job registered directly in the ``COMPLETED`` / inactive / uncommitted state —
so the existing ``verification_worktree`` / review / ``verify_development_job``
/ ``commit_verified_development_job`` / ``push_detached_development_commit``
gates accept it unchanged.

Safety invariants enforced here:

* **Exact provenance binding.** The exact canonical HEAD SHA is recorded and
  re-checked; a working-tree fingerprint is computed before the source is read,
  recomputed after the source collection, and recomputed once more immediately
  before success. Any drift (HEAD moved, a selected/protected file changed, the
  index changed) fails the capture and leaves no usable candidate.
* **Explicit, approved scope.** The caller must supply include and
  protected-exclude path sets that equal — as sets — the scope pinned in trusted
  local policy (``WorkingTreeCaptureScope``). The caller cannot widen, narrow,
  or guess the scope. Protected exclusions always override inclusions and never
  appear in the candidate manifest.
* **Capture only declared scope.** The candidate is materialised from the exact
  HEAD baseline and overlaid with only the selected changes; unrelated dirty
  files are never silently included.
* **Locks are not trusted alone.** The per-project lock serialises BridgeLab
  work, but external (non-BridgeLab) edits are still possible, so every capture
  re-validates source state from disk before succeeding.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import store
from .config import Settings
from .git import GitCommandError, run_git
from .jobs import DevelopmentJobError, DevelopmentJobManager, resolve_effort
from .models import (
    ProjectCapability,
    ProjectPolicy,
    WorkingTreeCaptureScope,
    _capture_scope_path,
)
from .registry import ProjectLockManager
from .verification import (
    VerificationError,
    _digest_candidate_paths,
    _git_bytes,
    _open_candidate_read,
    _validate_policy_path,
    candidate_changed_paths,
)
from .worktrees import GitWorktreeManager, JobWorktree, WorktreeError

_CAPTURE_ID_PREFIX = "wtc_"
# Canonical algorithm marker (mirrors the candidate-state marker in
# ``verification``). Versioned so the fingerprint scheme can evolve without
# ambiguity.
_FINGERPRINT_MARKER = b"local-dev-mcp-working-tree-state-v1\0"

# Porcelain v1 status codes that indicate the index changed relative to HEAD.
_INDEX_CHANGED = frozenset("AMTDRC")
# Porcelain v1 status codes that indicate the working tree changed relative to
# the index (excluding untracked, which is handled separately).
_TREE_CHANGED = frozenset("MTDA")


class WorkingTreeCaptureError(RuntimeError):
    """Raised when a working-tree capture cannot be performed safely."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _redact(value: Any, paths: tuple[Path, ...]) -> Any:
    """Replace absolute host paths with ``<redacted-path>`` in tool output."""
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


def _policy_fingerprint(project: ProjectPolicy) -> str:
    """A stable hash of the policy fields that bound a capture.

    Mirrors the runtime-verification policy fingerprint so an auditor can tie a
    captured candidate back to the exact policy under which it was approved.
    Includes ``working_tree_capture_scope`` so a scope change invalidates prior
    captures' policy binding.
    """
    scope = project.working_tree_capture_scope
    payload = {
        "project_id": project.id,
        "capabilities": sorted(c.value for c in project.capabilities),
        "commit_roots": sorted(project.commit_roots),
        "protected_paths": sorted(project.protected_paths),
        "working_tree_capture_scope": {
            "include_paths": sorted(scope.include_paths) if scope else [],
            "protected_exclude_paths": sorted(scope.protected_exclude_paths)
            if scope
            else [],
        }
        if scope
        else None,
    }
    return _sha256(_json_dumps(payload))


# --------------------------------------------------------------------------- #
# Scope / pattern helpers
# --------------------------------------------------------------------------- #


def _normalize_caller_paths(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Validate caller-supplied scope paths with the same rules as policy scope.

    Reuses ``_capture_scope_path`` (non-empty, relative, no ``..``, ``**`` only
    as a trailing full segment) and additionally rejects duplicates. Raises
    ``WorkingTreeCaptureError`` on any violation.
    """
    if values is None:
        raise WorkingTreeCaptureError("scope paths must be a list of strings")
    if isinstance(values, (str, bytes)):
        raise WorkingTreeCaptureError("scope paths must be a list of strings")
    paths: list[str] = []
    for raw in values:
        try:
            paths.append(_capture_scope_path(str(raw)))
        except ValueError as exc:
            raise WorkingTreeCaptureError(str(exc)) from exc
    if len(paths) != len(set(paths)):
        raise WorkingTreeCaptureError("scope paths must not contain duplicates")
    return tuple(paths)


def _pattern_matches(pattern: str, path: str) -> bool:
    """Match one scope pattern against one concrete changed path.

    A bare pattern (no ``**``) matches exactly that path (a concrete file). A
    trailing ``a/**`` matches ``a`` itself and everything beneath ``a/``. This
    keeps selection one-dimensional and deterministic: to capture a directory
    the policy must say ``dir/**``; a bare ``dir`` matches only a changed path
    literally named ``dir``.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(f"{prefix}/")
    return path == pattern


def _overlaps(left: str, right: str) -> bool:
    """Structural overlap of two include patterns (universe-independent).

    Two patterns overlap when there exists any concrete path matched by both.
    Used to reject ambiguous include scopes (e.g. ``docs/**`` and
    ``docs/x.md``) regardless of what is currently dirty.
    """
    if left == right:
        return True
    left_rec = left.endswith("/**")
    right_rec = right.endswith("/**")
    if left_rec and right_rec:
        a, b = left[:-3], right[:-3]
        return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")
    if left_rec:
        prefix = left[:-3]
        return right == prefix or right.startswith(f"{prefix}/")
    if right_rec:
        prefix = right[:-3]
        return left == prefix or left.startswith(f"{prefix}/")
    return False


def _validate_relative(path: str) -> str:
    """Validate one porcelain path is a safe, normalized relative path."""
    normalized = PurePosixPath(path.replace("\\", "/"))
    if (
        not path
        or path != normalized.as_posix()
        or normalized.is_absolute()
        or ".." in normalized.parts
        or path == "."
    ):
        raise WorkingTreeCaptureError(
            f"canonical working tree contains an invalid relative path: {path!r}"
        )
    return path


# --------------------------------------------------------------------------- #
# Porcelain + descriptors
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _PorcelainEntry:
    path: str
    x: str
    y: str

    @property
    def is_untracked(self) -> bool:
        return self.x == "?" and self.y == "?"

    @property
    def is_deleted(self) -> bool:
        return self.x == "D" or self.y == "D"

    @property
    def kind(self) -> str:
        if self.is_deleted:
            return "deleted"
        if self.x == "A" or self.is_untracked:
            return "added"
        return "modified"

    @property
    def staged(self) -> bool:
        if self.is_untracked:
            return False
        return self.x in _INDEX_CHANGED

    @property
    def unstaged(self) -> bool:
        if self.is_untracked:
            return True
        return self.y in _TREE_CHANGED


async def _porcelain_entries(root: Path, timeout: int) -> list[_PorcelainEntry]:
    """All changed paths in the canonical checkout (tracked + untracked)."""
    payload = await _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        timeout_seconds=timeout,
    )
    entries: list[_PorcelainEntry] = []
    for token in payload.split(b"\0"):
        if not token:
            continue
        # porcelain v1 -z record: "XY<space>path" (renames disabled → no
        # two-path records, so every non-empty token is one entry).
        if len(token) < 4 or token[2:3] != b" ":
            raise WorkingTreeCaptureError(
                "canonical working tree returned an unparseable status record"
            )
        try:
            path = token[3:].decode("utf-8", errors="strict")
            xy = token[:2].decode("ascii")
        except UnicodeDecodeError as exc:
            raise WorkingTreeCaptureError(
                "canonical working tree contains a non-UTF-8 path"
            ) from exc
        entries.append(_PorcelainEntry(path=_validate_relative(path), x=xy[0], y=xy[1]))
    return entries


@dataclass(slots=True)
class _PathDescriptor:
    """Per-path working-tree descriptor for the source fingerprint + overlay."""

    path: str
    kind: str
    staged: bool
    unstaged: bool
    mode: str | None
    size: int | None
    content_sha256: str | None
    symlink_target: str | None
    # Raw working-tree bytes for non-deleted paths, reused verbatim by the
    # overlay so the worktree receives exactly the bytes that were
    # fingerprinted. Never serialised into the fingerprint JSON.
    bytes: bytes | None

    def to_fingerprint_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "staged": self.staged,
            "unstaged": self.unstaged,
            "mode": self.mode,
            "size": self.size,
            "content_sha256": self.content_sha256,
            "symlink_target": self.symlink_target,
        }


async def _baseline_mode(root: Path, path: str, timeout: int) -> str:
    """Git mode of ``path`` at HEAD (used for deleted-path descriptors)."""
    payload = await _git_bytes(
        root, "ls-tree", "-z", "HEAD", "--", path, timeout_seconds=timeout
    )
    if b"\0" in payload:
        payload = payload.split(b"\0", 1)[0]
    if not payload:
        return "100644"
    # "<mode> <type> <object>\t<path>"
    return payload.split(b" ", 1)[0].decode("ascii", errors="replace") or "100644"


async def _describe_path(
    root: Path, entry: _PorcelainEntry, max_bytes: int, timeout: int
) -> _PathDescriptor:
    """Build the working-tree descriptor for one porcelain entry."""
    path = entry.path
    if entry.is_deleted:
        mode = await _baseline_mode(root, path, timeout)
        return _PathDescriptor(
            path=path,
            kind="deleted",
            staged=entry.staged,
            unstaged=entry.unstaged,
            mode=mode,
            size=None,
            content_sha256=None,
            symlink_target=None,
            bytes=None,
        )

    # Non-deleted: read the current working-tree bytes read-only. Symlinks and
    # non-regular leaves are refused by ``_open_candidate_read`` (which raises
    # ``VerificationError``), enforcing "reject changed symlinks".
    try:
        descriptor = _open_candidate_read(root, path, allow_missing=False)
    except VerificationError as exc:
        raise WorkingTreeCaptureError(str(exc)) from exc
    if descriptor is None:
        raise WorkingTreeCaptureError(
            f"selected path vanished from the working tree before read: {path}"
        )
    consumed = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkingTreeCaptureError(
                f"selected path is not a regular file: {path}"
            )
        if metadata.st_size > max_bytes:
            raise WorkingTreeCaptureError(
                f"selected path exceeds the capture byte limit: {path}"
            )
        git_mode = "100755" if metadata.st_mode & 0o111 else "100644"
        content_hash = hashlib.sha256()
        buffer = bytearray()
        handle = os.fdopen(descriptor, "rb")
        consumed = True
        total = 0
        with handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise WorkingTreeCaptureError(
                        f"selected path grew past the capture byte limit: {path}"
                    )
                content_hash.update(chunk)
                buffer.extend(chunk)
            after = os.fstat(handle.fileno())
        if (
            total > max_bytes
            or after.st_dev != metadata.st_dev
            or after.st_ino != metadata.st_ino
            or after.st_size != metadata.st_size
            or after.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise WorkingTreeCaptureError(
                f"selected path changed while it was read: {path}"
            )
        return _PathDescriptor(
            path=path,
            kind=entry.kind,
            staged=entry.staged,
            unstaged=entry.unstaged,
            mode=git_mode,
            size=total,
            content_sha256=content_hash.hexdigest(),
            symlink_target=None,
            bytes=bytes(buffer),
        )
    finally:
        if not consumed and descriptor is not None:
            os.close(descriptor)


def _resolve_manifest(
    entries: list[_PorcelainEntry],
    include_patterns: tuple[str, ...],
    protected_exclude_patterns: tuple[str, ...],
) -> tuple[list[_PorcelainEntry], list[_PorcelainEntry]]:
    """Partition changed paths into (selected, protected-excluded).

    ``selected`` = changed paths matching an include pattern and not matching a
    protected-exclude pattern. ``protected`` = changed paths matching a
    protected-exclude pattern (regardless of include). Protected exclusions
    always override inclusions. Both lists are sorted by path for determinism.
    """
    selected: list[_PorcelainEntry] = []
    protected: list[_PorcelainEntry] = []
    for entry in entries:
        matches_protected = any(
            _pattern_matches(p, entry.path) for p in protected_exclude_patterns
        )
        if matches_protected:
            protected.append(entry)
            continue
        if any(_pattern_matches(p, entry.path) for p in include_patterns):
            selected.append(entry)
    selected.sort(key=lambda e: e.path)
    protected.sort(key=lambda e: e.path)
    return selected, protected


def _working_tree_fingerprint(
    base_head: str,
    include_patterns: tuple[str, ...],
    protected_exclude_patterns: tuple[str, ...],
    selected: list[_PathDescriptor],
    protected: list[_PathDescriptor],
) -> str:
    """Canonical SHA-256 over the source working-tree state (no timestamps)."""
    payload = {
        "head": base_head.lower(),
        "include_paths": sorted(include_patterns),
        "protected_exclude_paths": sorted(protected_exclude_patterns),
        "selected": [d.to_fingerprint_dict() for d in selected],
        "protected_excluded": [d.to_fingerprint_dict() for d in protected],
    }
    return hashlib.sha256(
        _FINGERPRINT_MARKER + _json_dumps(payload).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Overlay (read-only on the source checkout)
# --------------------------------------------------------------------------- #


def _vet_target(root: Path, relative: str) -> Path:
    """Validate ``root/relative`` stays within ``root`` and traverses no symlink.

    Used for overlay writes/deletes inside the (trusted, freshly-created)
    candidate worktree. Existing ancestor components are lstat-walked and any
    symbolic link is refused; the resolved target must remain beneath the
    worktree root.
    """
    rel = PurePosixPath(relative)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise WorkingTreeCaptureError(
            f"invalid relative path in capture overlay: {relative!r}"
        )
    root_resolved = root.resolve(strict=True)
    cursor = root_resolved
    for part in rel.parts[:-1]:
        cursor = cursor / part
        try:
            metadata = os.lstat(cursor)
        except FileNotFoundError:
            break  # remaining ancestors do not exist yet (untracked new dirs)
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkingTreeCaptureError(
                "capture overlay refused a symbolic-link path"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkingTreeCaptureError(
                "capture overlay path traverses a non-directory"
            )
    target = root_resolved / relative
    if not target.resolve(strict=False).is_relative_to(root_resolved):
        raise WorkingTreeCaptureError("capture overlay path escapes the worktree")
    return target


def _overlay_path(
    worktree_root: Path, descriptor: _PathDescriptor
) -> None:
    """Apply one selected change onto the candidate worktree (read-only source).

    For non-deleted paths the cached, fingerprinted bytes are written with their
    git mode (exec bit preserved) and ``O_NOFOLLOW`` so a swapped symlink leaf
    is refused. For deletions the baseline file (present at HEAD) is removed.
    The worktree index is never touched, so it stays empty — exactly what
    ``verify_development_job`` requires.
    """
    target = _vet_target(worktree_root, descriptor.path)
    if descriptor.kind == "deleted":
        if target.is_symlink():
            raise WorkingTreeCaptureError(
                "capture overlay refused a symbolic-link leaf"
            )
        try:
            os.remove(target)
        except FileNotFoundError:
            pass  # already absent — idempotent
        except OSError as exc:
            raise WorkingTreeCaptureError(
                f"capture overlay could not remove deleted path: {descriptor.path}"
            ) from exc
        return

    parent = target.parent
    if not parent.exists():
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise WorkingTreeCaptureError(
                "capture overlay could not create parent directory"
            ) from exc
    if target.is_symlink():
        raise WorkingTreeCaptureError("capture overlay refused a symbolic-link leaf")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        raise WorkingTreeCaptureError(
            f"capture overlay could not open target for write: {descriptor.path}"
        ) from exc
    try:
        os.write(fd, descriptor.bytes or b"")
        # Preserve the git mode (exec bit) exactly as fingerprinted.
        mode = 0o755 if descriptor.mode == "100755" else 0o644
        os.fchmod(fd, mode)
    except OSError as exc:
        raise WorkingTreeCaptureError(
            f"capture overlay could not write target: {descriptor.path}"
        ) from exc
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class WorkingTreeCaptureManager:
    """Two-step preflight/capture of a dirty working tree into a candidate.

    Owns the additive ``working_tree_captures`` audit table in ``bridge.db``
    (append-only: one row per attempt, success or rejection). Successful
    captures also register a retained development job via
    ``DevelopmentJobManager.register_captured_candidate`` so every downstream
    gate accepts the candidate unchanged.
    """

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
        self._projects = projects
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")

    # -- scope -------------------------------------------------------------- #

    def _resolve_scope(
        self,
        project: ProjectPolicy,
        include_paths: list[str] | tuple[str, ...],
        protected_exclude_paths: list[str] | tuple[str, ...],
    ) -> WorkingTreeCaptureScope:
        scope = project.working_tree_capture_scope
        if scope is None:
            raise WorkingTreeCaptureError(
                "project has no pinned working_tree_capture_scope in policy"
            )
        if (
            ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE
            not in project.capabilities
        ):
            raise WorkingTreeCaptureError(
                "project does not have the working_tree_candidate_capture capability"
            )
        caller_include = _normalize_caller_paths(include_paths)
        caller_exclude = _normalize_caller_paths(protected_exclude_paths)
        if not caller_include:
            raise WorkingTreeCaptureError(
                "include_paths must be provided explicitly and match the approved scope"
            )
        if frozenset(caller_include) != frozenset(scope.include_paths):
            raise WorkingTreeCaptureError(
                "include_paths do not match the approved capture scope"
            )
        if frozenset(caller_exclude) != frozenset(scope.protected_exclude_paths):
            raise WorkingTreeCaptureError(
                "protected_exclude_paths do not match the approved capture scope"
            )
        # Structural overlap check on the approved include patterns.
        include_list = list(scope.include_paths)
        for i in range(len(include_list)):
            for j in range(i + 1, len(include_list)):
                if _overlaps(include_list[i], include_list[j]):
                    raise WorkingTreeCaptureError(
                        "approved include paths overlap ambiguously: "
                        f"{include_list[i]!r} and {include_list[j]!r}"
                    )
        return scope

    def _redaction_paths(self, project: ProjectPolicy) -> tuple[Path, ...]:
        return (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
        )

    # -- shared binding ----------------------------------------------------- #

    async def _bind_and_resolve(
        self,
        project: ProjectPolicy,
        scope: WorkingTreeCaptureScope,
        expected_base_head: str | None,
    ) -> tuple[str, list[_PorcelainEntry], list[_PorcelainEntry], list[_PathDescriptor], list[_PathDescriptor], str]:
        """Read canonical HEAD, validate scope, build manifests + fingerprint.

        Returns
        ``(base_head, selected_entries, protected_entries, selected_descriptors,
        protected_descriptors, working_tree_fingerprint)``. Read-only against
        the canonical checkout.
        """
        timeout = self.settings.git_command_timeout_seconds
        base_head = (
            await run_git(project.root, "rev-parse", "--verify", "HEAD", timeout_seconds=timeout)
        ).lower()
        if expected_base_head is not None and base_head != expected_base_head.lower():
            raise WorkingTreeCaptureError(
                f"canonical HEAD {base_head[:12]} does not match expected_base_head "
                f"{expected_base_head[:12]}"
            )

        entries = await _porcelain_entries(project.root, timeout)
        selected_entries, protected_entries = _resolve_manifest(
            entries, scope.include_paths, scope.protected_exclude_paths
        )

        # Every include pattern must select at least one changed path. A pattern
        # that matches only protected-excluded paths still "selected" something
        # (then excluded it); only a pattern matching nothing at all is rejected.
        for pattern in scope.include_paths:
            if not any(_pattern_matches(pattern, e.path) for e in entries):
                raise WorkingTreeCaptureError(
                    f"include path selects no changed paths: {pattern!r}"
                )

        if not selected_entries:
            raise WorkingTreeCaptureError(
                "capture scope selects no changes (all included paths are "
                "protected-excluded or unchanged)"
            )

        max_paths = self.settings.max_working_tree_capture_manifest_paths
        if len(selected_entries) > max_paths:
            raise WorkingTreeCaptureError(
                f"capture manifest exceeds the {max_paths}-path limit"
            )

        max_bytes = self.settings.max_working_tree_capture_file_bytes
        selected_descriptors = [
            await _describe_path(project.root, e, max_bytes, timeout)
            for e in selected_entries
        ]
        protected_descriptors = [
            await _describe_path(project.root, e, max_bytes, timeout)
            for e in protected_entries
        ]

        # Policy validation (commit roots / protected paths / hidden paths) on
        # each selected path, exactly as verification will require later.
        for entry in selected_entries:
            try:
                _validate_policy_path(project, entry.path)
            except VerificationError as exc:
                raise WorkingTreeCaptureError(str(exc)) from exc

        # Protected-excluded paths must never appear in the candidate manifest.
        protected_set = {e.path for e in protected_entries}
        for entry in selected_entries:
            if entry.path in protected_set:
                raise WorkingTreeCaptureError(
                    f"protected-excluded path leaked into the manifest: {entry.path!r}"
                )

        fingerprint = _working_tree_fingerprint(
            base_head,
            scope.include_paths,
            scope.protected_exclude_paths,
            selected_descriptors,
            protected_descriptors,
        )
        return (
            base_head,
            selected_entries,
            protected_entries,
            selected_descriptors,
            protected_descriptors,
            fingerprint,
        )

    def _manifest_view(self, descriptors: list[_PathDescriptor]) -> list[dict[str, Any]]:
        return [
            {
                "path": d.path,
                "kind": d.kind,
                "mode": d.mode,
                "size": d.size,
                "content_sha256": d.content_sha256,
                "symlink_target": d.symlink_target,
                "staged": d.staged,
                "unstaged": d.unstaged,
            }
            for d in descriptors
        ]

    # -- preflight ---------------------------------------------------------- #

    async def preflight(
        self,
        project: ProjectPolicy,
        include_paths: list[str] | tuple[str, ...],
        protected_exclude_paths: list[str] | tuple[str, ...],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Read-only preflight: bind HEAD, resolve scope, return the manifest.

        No worktree is created and no candidate is retained. The caller (or a
        governed job) echoes the returned ``base_head`` and
        ``working_tree_fingerprint`` into ``capture`` so the capture can prove
        it acted on the exact state the preflight observed.
        """
        scope = self._resolve_scope(project, include_paths, protected_exclude_paths)
        lock = await self._locks.for_project(project.id)
        async with lock:
            (
                base_head,
                _selected_entries,
                _protected_entries,
                selected_descriptors,
                protected_descriptors,
                fingerprint,
            ) = await self._bind_and_resolve(project, scope, expected_base_head)

            fingerprint_ok = True
            if (
                expected_working_tree_fingerprint is not None
                and expected_working_tree_fingerprint.lower() != fingerprint.lower()
            ):
                fingerprint_ok = False

            result = {
                "capture_id": None,
                "project_id": project.id,
                "base_head": base_head,
                "include_paths": sorted(scope.include_paths),
                "protected_exclude_paths": sorted(scope.protected_exclude_paths),
                "manifest": self._manifest_view(selected_descriptors),
                "protected_exclude_manifest": self._manifest_view(protected_descriptors),
                "working_tree_fingerprint": fingerprint,
                "expected_working_tree_fingerprint_ok": fingerprint_ok,
                "policy_fingerprint": _policy_fingerprint(project),
                "status": "preflight",
                "preflighted_at": _timestamp(),
            }
            return _redact(result, self._redaction_paths(project))

    # -- capture ------------------------------------------------------------ #

    async def capture(
        self,
        project: ProjectPolicy,
        include_paths: list[str] | tuple[str, ...],
        protected_exclude_paths: list[str] | tuple[str, ...],
        expected_base_head: str | None = None,
        expected_working_tree_fingerprint: str | None = None,
        *,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Capture the approved working-tree scope into a retained candidate.

        Two-step contract: when ``expected_base_head`` and
        ``expected_working_tree_fingerprint`` are supplied (from a prior
        preflight), the capture fails unless the canonical checkout still
        matches that exact state. On any drift, scope violation, or overlay
        failure the worktree is force-removed and no candidate is retained; a
        rejected audit row is persisted instead.
        """
        effort = resolve_effort(project, effort, self.settings)
        scope = self._resolve_scope(project, include_paths, protected_exclude_paths)
        redaction_paths = self._redaction_paths(project)
        policy_fingerprint = _policy_fingerprint(project)
        lock = await self._locks.for_project(project.id)

        async with lock:
            started_at = _timestamp()
            worktree: JobWorktree | None = None
            registered = False
            job_id = self._jobs.new_job_id()
            try:
                (
                    base_head,
                    selected_entries,
                    _protected_entries,
                    selected_descriptors,
                    protected_descriptors,
                    fingerprint_pre,
                ) = await self._bind_and_resolve(
                    project, scope, expected_base_head
                )

                if (
                    expected_working_tree_fingerprint is not None
                    and expected_working_tree_fingerprint.lower() != fingerprint_pre.lower()
                ):
                    raise WorkingTreeCaptureError(
                        "working tree fingerprint does not match the expected "
                        "fingerprint; the canonical checkout changed since preflight"
                    )

                # Create the detached candidate worktree at the exact bound HEAD.
                # Read-only against the canonical checkout (rev-parse + worktree
                # add --detach); the source working tree and index are untouched.
                worktree = await self._worktrees.create_detached_at_head(
                    project, job_id, base_head
                )

                # Overlay only the selected, fingerprinted bytes. The source is
                # never written; the worktree index is never staged.
                for descriptor in selected_descriptors:
                    _overlay_path(worktree.path, descriptor)

                # Re-validate the source state from disk: HEAD must be unchanged
                # and the working-tree fingerprint must be identical. This is
                # the guard against external (non-BridgeLab) edits that the
                # project lock cannot prevent.
                (
                    post_head,
                    _post_selected,
                    _post_protected,
                    post_selected_desc,
                    post_protected_desc,
                    fingerprint_post,
                ) = await self._bind_and_resolve(project, scope, None)
                if post_head.lower() != base_head.lower():
                    raise WorkingTreeCaptureError(
                        "canonical HEAD moved during capture; refusing to retain "
                        "the candidate"
                    )
                if fingerprint_post.lower() != fingerprint_pre.lower():
                    raise WorkingTreeCaptureError(
                        "working tree changed during capture (selected or "
                        "protected-excluded paths drifted); refusing to retain "
                        "the candidate"
                    )
                # The post descriptors must be byte-identical to the pre
                # descriptors we already overlaid.
                _assert_descriptors_stable(selected_descriptors, post_selected_desc)
                _assert_descriptors_stable(protected_descriptors, post_protected_desc)

                # Compute the candidate fingerprint exactly as verification will,
                # and require the worktree's changed-path set to equal the
                # selected manifest. This guarantees the captured candidate is
                # indistinguishable from a Claude-produced one at the gate.
                changed = await candidate_changed_paths(self.settings, worktree)
                selected_paths = tuple(d.path for d in selected_descriptors)
                if set(changed) != set(selected_paths):
                    raise WorkingTreeCaptureError(
                        "candidate worktree changed-path set does not match the "
                        "capture manifest"
                    )
                candidate_fingerprint = await _digest_candidate_paths(
                    self.settings, project, worktree, changed, validate_policy=True
                )

                candidate = {
                    "origin": "working_tree_capture",
                    "base_head": base_head,
                    "manifest": self._manifest_view(selected_descriptors),
                    "protected_exclude_manifest": self._manifest_view(
                        protected_descriptors
                    ),
                    "working_tree_fingerprint": fingerprint_pre,
                    "candidate_fingerprint": candidate_fingerprint,
                }
                job = await self._jobs.register_captured_candidate(
                    project, job_id, worktree, candidate, effort=effort
                )
                registered = True

                captured_at = _timestamp()
                result = {
                    "capture_id": job_id,
                    "job_id": job_id,
                    "candidate_id": job_id,
                    "project_id": project.id,
                    "base_head": base_head,
                    "include_paths": sorted(scope.include_paths),
                    "protected_exclude_paths": sorted(scope.protected_exclude_paths),
                    "manifest": self._manifest_view(selected_descriptors),
                    "protected_exclude_manifest": self._manifest_view(
                        protected_descriptors
                    ),
                    "working_tree_fingerprint": fingerprint_pre,
                    "candidate_fingerprint": candidate_fingerprint,
                    "policy_fingerprint": policy_fingerprint,
                    "started_at": started_at,
                    "captured_at": captured_at,
                    "status": "captured",
                    "effort": job.effort,
                }

                self._persist_audit(
                    project_id=project.id,
                    capture_id=job_id,
                    job_id=job_id,
                    base_head=base_head,
                    working_tree_fingerprint=fingerprint_pre,
                    candidate_fingerprint=candidate_fingerprint,
                    scope=scope,
                    selected_descriptors=selected_descriptors,
                    protected_descriptors=protected_descriptors,
                    status="captured",
                    error=None,
                    captured_at=captured_at,
                )
                return _redact(result, redaction_paths)

            except (WorkingTreeCaptureError, WorktreeError, DevelopmentJobError, VerificationError) as exc:
                # Fail safe: never retain a partial candidate. The worktree is
                # force-removed unless the job was already registered (in which
                # case the candidate is durably retained and owned by the job
                # manager).
                self._persist_audit(
                    project_id=project.id,
                    capture_id=_CAPTURE_ID_PREFIX + secrets.token_hex(16),
                    job_id="",
                    base_head="",
                    working_tree_fingerprint="",
                    candidate_fingerprint="",
                    scope=scope,
                    selected_descriptors=[],
                    protected_descriptors=[],
                    status="rejected",
                    error=str(exc),
                    captured_at=_timestamp(),
                )
                raise WorkingTreeCaptureError(str(exc)) from exc
            finally:
                if worktree is not None and not registered:
                    await self._force_remove_worktree(project, worktree)

    async def _force_remove_worktree(self, project: ProjectPolicy, worktree: JobWorktree) -> None:
        timeout = self.settings.git_command_timeout_seconds
        try:
            await run_git(
                project.root,
                "worktree",
                "remove",
                "--force",
                "--",
                str(worktree.path),
                timeout_seconds=timeout,
            )
        except (GitCommandError, OSError):
            # Best-effort prune; the worktree directory may already be gone.
            try:
                await run_git(
                    project.root,
                    "worktree",
                    "prune",
                    timeout_seconds=timeout,
                )
            except (GitCommandError, OSError):
                pass

    # -- persistence / audit ------------------------------------------------ #

    def _persist_audit(
        self,
        *,
        project_id: str,
        capture_id: str,
        job_id: str,
        base_head: str,
        working_tree_fingerprint: str,
        candidate_fingerprint: str,
        scope: WorkingTreeCaptureScope,
        selected_descriptors: list[_PathDescriptor],
        protected_descriptors: list[_PathDescriptor],
        status: str,
        error: str | None,
        captured_at: str,
    ) -> None:
        row = {
            "capture_id": capture_id,
            "project_id": project_id,
            "job_id": job_id,
            "base_head": base_head,
            "working_tree_fingerprint": working_tree_fingerprint,
            "candidate_fingerprint": candidate_fingerprint,
            "include_paths_json": json.dumps(
                sorted(scope.include_paths), ensure_ascii=False
            ),
            "protected_exclude_paths_json": json.dumps(
                sorted(scope.protected_exclude_paths), ensure_ascii=False
            ),
            "manifest_json": json.dumps(
                [d.to_fingerprint_dict() for d in selected_descriptors],
                ensure_ascii=False,
            ),
            "protected_exclude_manifest_json": json.dumps(
                [d.to_fingerprint_dict() for d in protected_descriptors],
                ensure_ascii=False,
            ),
            "status": status,
            "error": error,
            "captured_at": captured_at,
        }
        store.insert_working_tree_capture_row(self._db, row)

    def audit(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return persisted capture rows as dicts, oldest first (read-only)."""
        return store.load_working_tree_capture_rows(self._db, project_id)


def _assert_descriptors_stable(
    pre: list[_PathDescriptor], post: list[_PathDescriptor]
) -> None:
    """Fail if the post-overlay source descriptors differ from the pre ones."""
    pre_map = {d.path: d for d in pre}
    post_map = {d.path: d for d in post}
    if set(pre_map) != set(post_map):
        raise WorkingTreeCaptureError(
            "the set of changed paths drifted during capture"
        )
    for path, pre_desc in pre_map.items():
        post_desc = post_map[path]
        if (
            pre_desc.kind != post_desc.kind
            or pre_desc.staged != post_desc.staged
            or pre_desc.unstaged != post_desc.unstaged
            or pre_desc.mode != post_desc.mode
            or pre_desc.size != post_desc.size
            or pre_desc.content_sha256 != post_desc.content_sha256
        ):
            raise WorkingTreeCaptureError(
                f"selected or protected path changed during capture: {path!r}"
            )