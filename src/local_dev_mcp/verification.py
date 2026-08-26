from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import signal
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import store
from .config import Settings
from .git import GitCommandError, run_git
from .jobs import DevelopmentJobError, DevelopmentJobManager
from .models import ProjectPolicy, SupplyChainCommand, VerificationCommand
from .registry import ProjectLockManager
from .worktrees import GitWorktreeManager, JobWorktree, WorktreeError


class VerificationError(RuntimeError):
    """Raised when a candidate cannot be safely verified or committed."""


MAX_COMMIT_PATHS = 500
_FINGERPRINT_MARKER = b"local-dev-mcp-candidate-state-v1\0"


def _stderr_text(result: dict[str, Any]) -> str:
    """Extract the bounded stderr text from a command result dict (or '')."""
    stderr = result.get("stderr")
    if isinstance(stderr, dict):
        text = stderr.get("text")
        return text if isinstance(text, str) else ""
    return stderr if isinstance(stderr, str) else ""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big") + value


def _is_beneath(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(f"{root}/")


def _decode_paths(payload: bytes) -> tuple[str, ...]:
    if not payload:
        return ()
    values: list[str] = []
    for raw in payload.rstrip(b"\0").split(b"\0"):
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise VerificationError("candidate paths must be valid UTF-8") from exc
        normalized = PurePosixPath(value.replace("\\", "/"))
        if (
            not value
            or value != normalized.as_posix()
            or normalized.is_absolute()
            or ".." in normalized.parts
            or value == "."
        ):
            raise VerificationError("candidate contains an invalid relative path")
        values.append(value)
    return tuple(values)


async def _git_bytes(
    root: Path,
    *arguments: str,
    timeout_seconds: int,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
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
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise VerificationError("a bounded Git command timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-2000:]
        raise VerificationError(
            f"a bounded Git command failed with exit code {process.returncode}: {detail}"
        )
    return stdout


async def _digest_git_blob(
    root: Path,
    object_id: str,
    digest: Any,
    *,
    timeout_seconds: int,
) -> tuple[int, str]:
    size_payload = await _git_bytes(
        root,
        "cat-file",
        "-s",
        object_id,
        timeout_seconds=timeout_seconds,
    )
    try:
        expected_size = int(size_payload.strip())
    except ValueError as exc:
        raise VerificationError("Git returned an invalid staged blob size") from exc

    process = await asyncio.create_subprocess_exec(
        "git",
        "cat-file",
        "blob",
        object_id,
        cwd=str(root),
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    content_hash = hashlib.sha256()
    actual_size = 0

    async def consume() -> bytes:
        nonlocal actual_size
        while True:
            chunk = await process.stdout.read(1024 * 1024)
            if not chunk:
                break
            actual_size += len(chunk)
            content_hash.update(chunk)
            digest.update(chunk)
        return await process.stderr.read()

    try:
        stderr = await asyncio.wait_for(consume(), timeout=timeout_seconds)
        await process.wait()
    except asyncio.TimeoutError as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise VerificationError("a bounded Git blob read timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-2000:]
        raise VerificationError(f"Git could not read a staged blob: {detail}")
    if actual_size != expected_size:
        raise VerificationError("Git staged blob size changed while it was read")
    return actual_size, content_hash.hexdigest()


def _safe_path(
    root: Path,
    relative: str,
    *,
    allow_missing: bool,
    directory: bool = False,
) -> Path | None:
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        # Do not embed ``exc``: its ``.filename`` is the absolute worktree path,
        # which must never reach tool output. Surface a path-free message.
        raise VerificationError("candidate worktree could not be opened") from exc
    parts = PurePosixPath(relative).parts
    try:
        for component in parts[:-1]:
            try:
                metadata = os.stat(
                    component, dir_fd=descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise VerificationError("candidate path no longer exists") from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise VerificationError(
                    "candidate paths must not traverse symbolic links or non-directories"
                )
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise VerificationError("candidate path no longer exists") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError("changed symbolic links are not allowed")
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode):
            kind = "directories" if directory else "regular files or deletions"
            raise VerificationError(f"candidate paths must be {kind}")
        return root.joinpath(*parts)
    except OSError as exc:
        raise VerificationError(f"candidate path could not be inspected: {exc}") from exc
    finally:
        os.close(descriptor)


def _open_candidate_read(
    root: Path,
    relative: str,
    *,
    allow_missing: bool,
) -> int | None:
    """Open one candidate regular file anchored end-to-end to ``root``.

    Like ``_safe_path`` this walks the directory chain with
    ``O_DIRECTORY|O_NOFOLLOW`` against a parent ``dir_fd`` so no parent can be a
    symbolic link. Unlike ``_safe_path`` it does NOT return a path string for the
    caller to re-resolve: it opens the leaf with ``O_NOFOLLOW`` *relative to the
    already-validated parent directory descriptor*. The open therefore never
    re-resolves from the filesystem root, so a parent directory swapped for a
    symlink in the window between validation and the open cannot redirect the
    read outside the worktree. ``O_NOFOLLOW`` on the leaf additionally refuses a
    leaf that became a symlink in that window. Returns the open file descriptor,
    or ``None`` for a deletion when ``allow_missing``. Raises ``VerificationError``
    on a symlink, non-regular, or missing path (``allow_missing=False``); error
    filenames are the relative leaf, never the absolute worktree path.
    """
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        # Do not embed ``exc``: its ``.filename`` is the absolute worktree path,
        # which must never reach tool output. Surface a path-free message.
        raise VerificationError("candidate worktree could not be opened") from exc
    parts = PurePosixPath(relative).parts
    try:
        for component in parts[:-1]:
            try:
                metadata = os.stat(
                    component, dir_fd=descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise VerificationError("candidate path no longer exists") from None
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise VerificationError(
                    "candidate paths must not traverse symbolic links or non-directories"
                )
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        leaf = parts[-1]
        try:
            metadata = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise VerificationError("candidate path no longer exists") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError("changed symbolic links are not allowed")
        if not stat.S_ISREG(metadata.st_mode):
            raise VerificationError("candidate paths must be regular files or deletions")
        try:
            return os.open(
                leaf,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except FileNotFoundError:
            if allow_missing:
                return None
            raise VerificationError("candidate path no longer exists") from None
    except OSError as exc:
        raise VerificationError(f"candidate path could not be inspected: {exc}") from exc
    finally:
        os.close(descriptor)


def _validate_policy_path(project: ProjectPolicy, path: str) -> None:
    if any(_is_beneath(path, protected) for protected in project.protected_paths):
        raise VerificationError(f"candidate path is protected: {path}")
    if not any(_is_beneath(path, root) for root in project.commit_roots):
        raise VerificationError(f"candidate path is outside approved commit roots: {path}")
    if any(part.startswith(".") for part in PurePosixPath(path).parts):
        explicitly_allowed = any(
            root != "."
            and any(part.startswith(".") for part in PurePosixPath(root).parts)
            and _is_beneath(path, root)
            for root in project.commit_roots
        )
        if not explicitly_allowed:
            raise VerificationError(
                f"hidden candidate path requires an explicit commit root: {path}"
            )


@dataclass(frozen=True, slots=True)
class CandidateState:
    base_head: str
    fingerprint: str
    paths: tuple[str, ...]


@dataclass(slots=True)
class VerificationRecord:
    verification_id: str
    project_id: str
    job_id: str
    base_head: str
    diff_fingerprint: str
    paths: tuple[str, ...]
    verified_at: str
    commands: tuple[dict[str, Any], ...]
    used: bool = False


def _record_json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def record_to_row(record: VerificationRecord) -> dict[str, Any]:
    """Serialize a verification record to a dict row for the durable store."""
    return {
        "verification_id": record.verification_id,
        "project_id": record.project_id,
        "job_id": record.job_id,
        "base_head": record.base_head,
        "diff_fingerprint": record.diff_fingerprint,
        "paths_json": json.dumps(list(record.paths), ensure_ascii=False),
        "verified_at": record.verified_at,
        "used": 1 if record.used else 0,
        "commands_json": json.dumps(list(record.commands), ensure_ascii=False),
    }


def record_from_row(row: dict[str, Any]) -> VerificationRecord | None:
    """Rebuild a verification record from a dict row, or None if unparseable."""
    paths = _record_json_loads(row.get("paths_json"))
    commands = _record_json_loads(row.get("commands_json"))
    if not isinstance(paths, list) or not isinstance(commands, list):
        return None
    try:
        return VerificationRecord(
            verification_id=row["verification_id"],
            project_id=row["project_id"],
            job_id=row["job_id"],
            base_head=row["base_head"],
            diff_fingerprint=row["diff_fingerprint"],
            paths=tuple(paths),
            verified_at=row["verified_at"],
            commands=tuple(commands),
            used=bool(row.get("used", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def candidate_changed_paths(
    settings: Settings,
    worktree: JobWorktree,
) -> tuple[str, ...]:
    """Discover the candidate's changed paths (tracked edits + untracked files).

    The index-empty guard is intentionally NOT part of this helper: it is a
    verification-specific precondition (``commit`` requires an empty index),
    not a property of the changed-path set itself. Callers that need the
    guard apply it separately. Returns paths in the same sorted, de-duplicated
    order used by the candidate fingerprint so review and verification agree
    on exactly one notion of "what the candidate changed".
    """
    timeout = settings.git_command_timeout_seconds
    tracked_payload, untracked_payload = await asyncio.gather(
        _git_bytes(
            worktree.path,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "HEAD",
            "--",
            timeout_seconds=timeout,
        ),
        _git_bytes(
            worktree.path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            timeout_seconds=timeout,
        ),
    )
    paths = tuple(sorted(set(_decode_paths(tracked_payload) + _decode_paths(untracked_payload))))
    if not paths:
        raise VerificationError("candidate has no changes to verify")
    if len(paths) > MAX_COMMIT_PATHS:
        raise VerificationError(
            f"candidate exceeds the {MAX_COMMIT_PATHS}-path verification limit"
        )
    return paths


async def _digest_candidate_paths(
    settings: Settings,
    project: ProjectPolicy | None,
    worktree: JobWorktree,
    paths: tuple[str, ...],
    *,
    validate_policy: bool,
) -> str:
    """Content-address the current working-tree state of ``paths``.

    Computes the canonical SHA-256 candidate fingerprint over the worktree's
    ``base_head`` plus each path's ``HEAD`` baseline and current working-tree
    bytes/mode/size. This is the shared core of ``_candidate_state`` (full
    verification) and ``partial_fingerprint`` (mid-flight progress sampler).
    It does NOT enforce the empty-index guard or the empty-paths guard — those
    are verification preconditions owned by the callers.

    When ``validate_policy`` is true and ``project`` is given, each path is
    checked against ``commit_roots``/``protected_paths`` exactly as
    verification requires. A symlink or submodule entry, a non-regular leaf,
    or a file that changes while it is read raises ``VerificationError`` so a
    verification caller fails closed; a progress-sampling caller passes
    ``validate_policy=False`` and wraps the call in its own try/except.
    """
    timeout = settings.git_command_timeout_seconds
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_MARKER)
    digest.update(_frame(worktree.base_head.encode("ascii")))
    for relative in paths:
        if validate_policy and project is not None:
            _validate_policy_path(project, relative)
        file_descriptor = _open_candidate_read(
            worktree.path, relative, allow_missing=True
        )
        # ``file_descriptor`` is ours until ``os.fdopen`` takes ownership below.
        # Close it on any error path in that window (e.g. an ``ls-tree`` timeout or
        # a non-regular leaf) so the open never leaks; once ``consumed`` is set the
        # ``with`` block owns the fd and the ``finally`` is a no-op.
        consumed = False
        try:
            baseline = await _git_bytes(
                worktree.path,
                "ls-tree",
                "-z",
                "HEAD",
                "--",
                relative,
                timeout_seconds=timeout,
            )
            if b"\0" in baseline:
                baseline = baseline.split(b"\0", 1)[0]
            if baseline.startswith((b"120000 ", b"160000 ")):
                raise VerificationError(
                    "changed symbolic links and submodules are not allowed"
                )
            digest.update(_frame(relative.encode("utf-8")))
            digest.update(_frame(baseline))
            if file_descriptor is None:
                digest.update(_frame(b"deleted"))
                continue
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationError(
                    "changed paths must be regular files or deletions"
                )
            git_mode = b"100755" if metadata.st_mode & 0o111 else b"100644"
            digest.update(_frame(git_mode))
            digest.update(_frame(str(metadata.st_size).encode("ascii")))
            content_hash = hashlib.sha256()
            actual_size = 0
            handle = os.fdopen(file_descriptor, "rb")
            consumed = True
            with handle:
                while chunk := handle.read(1024 * 1024):
                    actual_size += len(chunk)
                    content_hash.update(chunk)
                    digest.update(chunk)
                after_metadata = os.fstat(handle.fileno())
            if (
                actual_size != metadata.st_size
                or after_metadata.st_dev != metadata.st_dev
                or after_metadata.st_ino != metadata.st_ino
                or after_metadata.st_size != metadata.st_size
                or after_metadata.st_mtime_ns != metadata.st_mtime_ns
            ):
                raise VerificationError(
                    "candidate file changed while it was fingerprinted"
                )
            digest.update(_frame(content_hash.hexdigest().encode("ascii")))
        finally:
            if not consumed and file_descriptor is not None:
                os.close(file_descriptor)
    return digest.hexdigest()


async def _candidate_state(
    settings: Settings,
    project: ProjectPolicy,
    worktree: JobWorktree,
) -> CandidateState:
    timeout = settings.git_command_timeout_seconds
    staged = await _git_bytes(
        worktree.path,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-renames",
        "HEAD",
        "--",
        timeout_seconds=timeout,
    )
    if staged:
        raise VerificationError("candidate index must be empty before verification")

    paths = await candidate_changed_paths(settings, worktree)
    fingerprint = await _digest_candidate_paths(
        settings, project, worktree, paths, validate_policy=True
    )
    return CandidateState(
        base_head=worktree.base_head,
        fingerprint=fingerprint,
        paths=paths,
    )


async def partial_fingerprint(
    settings: Settings,
    worktree: JobWorktree,
    paths: tuple[str, ...],
    *,
    project: ProjectPolicy | None = None,
) -> str:
    """Best-effort content-addressed snapshot of the current changed set.

    Computes the same digest as ``_candidate_state`` over ``base_head`` plus
    each path's working-tree bytes, but is lenient: it never enforces the
    empty-index or empty-paths guards and (when ``project`` is omitted) skips
    policy validation, so the progress sampler can record a checkpoint
    mid-flight without aborting the job. When ``paths`` is empty the digest
    covers only ``base_head`` (a stable "no changes yet" value). A caller that
    needs crash-safety wraps this in try/except and records
    ``partial_fingerprint=None`` on failure.
    """
    return await _digest_candidate_paths(
        settings, project, worktree, paths, validate_policy=False
    )


async def _staged_state(
    settings: Settings,
    project: ProjectPolicy,
    worktree: JobWorktree,
    paths: tuple[str, ...],
) -> CandidateState:
    timeout = settings.git_command_timeout_seconds
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_MARKER)
    digest.update(_frame(worktree.base_head.encode("ascii")))
    for relative in paths:
        _validate_policy_path(project, relative)
        baseline = await _git_bytes(
            worktree.path,
            "ls-tree",
            "-z",
            "HEAD",
            "--",
            relative,
            timeout_seconds=timeout,
        )
        if b"\0" in baseline:
            baseline = baseline.split(b"\0", 1)[0]
        if baseline.startswith((b"120000 ", b"160000 ")):
            raise VerificationError("changed symbolic links and submodules are not allowed")
        digest.update(_frame(relative.encode("utf-8")))
        digest.update(_frame(baseline))

        index_entry = await _git_bytes(
            worktree.path,
            "ls-files",
            "--stage",
            "-z",
            "--",
            relative,
            timeout_seconds=timeout,
        )
        if not index_entry:
            digest.update(_frame(b"deleted"))
            continue
        metadata, _, indexed_path = index_entry.rstrip(b"\0").partition(b"\t")
        fields = metadata.split()
        if len(fields) != 3 or fields[2] != b"0":
            raise VerificationError("candidate index contains an unresolved entry")
        try:
            decoded_indexed_path = indexed_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise VerificationError("candidate paths must be valid UTF-8") from exc
        if decoded_indexed_path != relative or fields[0] not in {b"100644", b"100755"}:
            raise VerificationError("candidate index contains an unexpected path type")
        digest.update(_frame(fields[0]))
        object_id = fields[1].decode("ascii")
        size_payload = await _git_bytes(
            worktree.path,
            "cat-file",
            "-s",
            object_id,
            timeout_seconds=timeout,
        )
        try:
            blob_size = int(size_payload.strip())
        except ValueError as exc:
            raise VerificationError("Git returned an invalid staged blob size") from exc
        digest.update(_frame(str(blob_size).encode("ascii")))
        actual_size, content_sha256 = await _digest_git_blob(
            worktree.path,
            object_id,
            digest,
            timeout_seconds=timeout,
        )
        if actual_size != blob_size:
            raise VerificationError("Git staged blob size changed while it was read")
        digest.update(_frame(content_sha256.encode("ascii")))
    return CandidateState(
        base_head=worktree.base_head,
        fingerprint=digest.hexdigest(),
        paths=paths,
    )


async def committed_state(
    settings: Settings,
    project: ProjectPolicy,
    worktree: JobWorktree,
    commit_sha: str,
    base_head: str,
    paths: tuple[str, ...],
) -> CandidateState:
    """Re-derive the candidate fingerprint directly from a committed git object.

    This is the publication counterpart of ``_staged_state``: instead of reading
    the index, it reads the commit's tree with ``git ls-tree -z <commit_sha> --
    <path>`` and frames each entry against the ``base_head`` baseline exactly as
    ``_staged_state`` frames the staged index against ``HEAD``. A path absent
    from the commit is a deletion. Because ``commit_verified_development_job``
    stages the verified paths and commits a tree equal to that staged state, the
    fingerprint computed here equals the one bound to the verification record, so
    publication can prove the committed object realizes the verified candidate
    without trusting the jobs table's ``committed_sha`` alone.
    """
    timeout = settings.git_command_timeout_seconds
    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_MARKER)
    digest.update(_frame(base_head.encode("ascii")))
    for relative in paths:
        _validate_policy_path(project, relative)
        baseline = await _git_bytes(
            worktree.path,
            "ls-tree",
            "-z",
            base_head,
            "--",
            relative,
            timeout_seconds=timeout,
        )
        if b"\0" in baseline:
            baseline = baseline.split(b"\0", 1)[0]
        if baseline.startswith((b"120000 ", b"160000 ")):
            raise VerificationError("changed symbolic links and submodules are not allowed")
        digest.update(_frame(relative.encode("utf-8")))
        digest.update(_frame(baseline))

        entry = await _git_bytes(
            worktree.path,
            "ls-tree",
            "-z",
            commit_sha,
            "--",
            relative,
            timeout_seconds=timeout,
        )
        if b"\0" in entry:
            entry = entry.split(b"\0", 1)[0]
        if entry.startswith((b"120000 ", b"160000 ")):
            raise VerificationError("commit contains a symbolic link or submodule")
        if not entry:
            digest.update(_frame(b"deleted"))
            continue
        metadata, _, _indexed_path = entry.rstrip(b"\0").partition(b"\t")
        fields = metadata.split()
        # ``git ls-tree`` emits "<mode> <type> <oid>\t<path>".
        if len(fields) != 3:
            raise VerificationError("commit contains an unexpected tree entry")
        if fields[0] not in {b"100644", b"100755"}:
            raise VerificationError("commit contains an unexpected path type")
        digest.update(_frame(fields[0]))
        object_id = fields[2].decode("ascii")
        size_payload = await _git_bytes(
            worktree.path,
            "cat-file",
            "-s",
            object_id,
            timeout_seconds=timeout,
        )
        try:
            blob_size = int(size_payload.strip())
        except ValueError as exc:
            raise VerificationError("Git returned an invalid committed blob size") from exc
        digest.update(_frame(str(blob_size).encode("ascii")))
        actual_size, content_sha256 = await _digest_git_blob(
            worktree.path, object_id, digest, timeout_seconds=timeout
        )
        if actual_size != blob_size:
            raise VerificationError("Git committed blob size changed while it was read")
        digest.update(_frame(content_sha256.encode("ascii")))
    return CandidateState(
        base_head=base_head,
        fingerprint=digest.hexdigest(),
        paths=paths,
    )


async def _read_stream(
    stream: asyncio.StreamReader,
    limit: int,
) -> dict[str, Any]:
    # Keep one maximum filesystem path beyond the public limit so redaction can
    # still match a path that straddles the configured output boundary.
    capture_limit = limit + 8192
    captured = bytearray()
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        remaining = capture_limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return {
        "text": bytes(captured).decode("utf-8", errors="replace"),
        "bytes": total,
        "sha256": digest.hexdigest(),
        "truncated": total > limit,
    }


def _bound_redacted_output(value: dict[str, Any], limit: int) -> dict[str, Any]:
    for stream_name in ("stdout", "stderr"):
        stream = value.get(stream_name)
        if not isinstance(stream, dict) or not isinstance(stream.get("text"), str):
            continue
        encoded = stream["text"].encode("utf-8")
        if len(encoded) > limit:
            stream["text"] = encoded[:limit].decode("utf-8", errors="ignore")
            stream["truncated"] = True
    return value


def _python_paths(root: Path, entries: Iterable[str]) -> str | None:
    resolved: list[str] = []
    for entry in entries:
        target = _safe_path(root, entry, allow_missing=False, directory=True)
        assert target is not None
        resolved.append(str(target))
    return os.pathsep.join(resolved) if resolved else None


async def _run_exec(
    settings: Settings,
    worktree: JobWorktree,
    *,
    command_id: str,
    argv: list[str],
    extra_env: dict[str, str] | None = None,
    start_error_prefix: str = "executable could not start",
) -> dict[str, Any]:
    """Run a fixed-argv subprocess in the candidate worktree and bound its output.

    Shared by the verification commands and the M-CC-11 supply-chain scans.
    The environment is deterministic (M-CC-09) and stripped of tokens/certs; no
    shell is involved. A non-zero exit code is reported to the caller, which
    decides whether it is a gate rejection.
    """
    expanded = list(argv)
    if expanded[0] == "{python}":
        expanded[0] = sys.executable
    environment: dict[str, str] = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        # M-CC-09: deterministic env so the same candidate + command always
        # yields the same result (the hermetic verification lab).
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": "0",
        "PYTHONHASHSEED": "0",
    }
    if extra_env:
        environment.update(extra_env)

    try:
        process = await asyncio.create_subprocess_exec(
            *expanded,
            cwd=str(worktree.path),
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return {
            "command_id": command_id,
            "status": "failed",
            "exit_code": None,
            "error": f"{start_error_prefix}: {exc}",
        }

    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(
        _read_stream(process.stdout, settings.max_verification_output_bytes)
    )
    stderr_task = asyncio.create_task(
        _read_stream(process.stderr, settings.max_verification_output_bytes)
    )
    timed_out = False
    try:
        await asyncio.wait_for(
            process.wait(), timeout=settings.verification_command_timeout_seconds
        )
    except asyncio.CancelledError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        raise
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return {
        "command_id": command_id,
        "status": "timed_out"
        if timed_out
        else ("passed" if process.returncode == 0 else "failed"),
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _sandbox_argv(worktree: JobWorktree, argv: list[str]) -> list[str]:
    """Wrap a fixed-argv command in a bubblewrap (bwrap) syscall sandbox.

    M-CC-11: repo-controlled verification scripts run under a lightweight
    sandbox rather than with the bridge's privilege. The bridge invokes the
    sandbox runner (bwrap) with a fixed argument vector — never the sandbox
    script directly — with ``--unshare-net`` (network none), a read-only root
    bind, and a frozen clock (``SOURCE_DATE_EPOCH``). The candidate worktree is
    bound read-only so a repo-controlled script cannot modify the candidate
    state it is supposed to verify. ``--die-with-parent`` ties the sandbox's
    lifetime to the bridge so a killed bridge cannot orphan a sandboxed process.

    Only the candidate worktree and the minimal interpreter runtime (``/usr``,
    ``/lib``, ``/lib64``, ``/bin``, ``/etc/alternatives``) are bound read-only —
    never the whole host root — so a malicious repo-controlled script cannot
    read host secrets (``/etc/shadow``, ``.env``, project config, ``/home``).
    ``/dev/null`` is the only device exposed.
    """
    return [
        "bwrap",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-pid",
        "--ro-bind",
        str(worktree.path),
        str(worktree.path),
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--ro-bind",
        "/bin",
        "/bin",
        "--ro-bind",
        "/etc/alternatives",
        "/etc/alternatives",
        "--proc",
        "/proc",
        "--dev-bind",
        "/dev/null",
        "/dev/null",
        "--tmpfs",
        "/tmp",
        "--setenv",
        "SOURCE_DATE_EPOCH",
        "0",
        "--die-with-parent",
        *argv,
    ]


async def _run_command(
    settings: Settings,
    worktree: JobWorktree,
    command: VerificationCommand,
) -> dict[str, Any]:
    python_path = _python_paths(worktree.path, command.python_path)
    extra_env = {"PYTHONPATH": python_path} if python_path is not None else None
    argv = list(command.argv)
    if command.sandbox:
        argv = _sandbox_argv(worktree, argv)
    return await _run_exec(
        settings,
        worktree,
        command_id=command.id,
        argv=argv,
        extra_env=extra_env,
        start_error_prefix="verification executable could not start",
    )


async def _run_supply_chain_scan(
    settings: Settings,
    worktree: JobWorktree,
    scan: SupplyChainCommand,
) -> dict[str, Any]:
    """Run one severity-gated supply-chain scan with a fixed argument vector.

    M-CC-11: a deterministic scanner (e.g. ``grype ... --fail-on=high``,
    ``pip-audit ...``) is invoked with fixed argv (no shell) inside the
    candidate worktree. A non-zero exit code means the gate rejects the commit
    (exactly how grype's ``--fail-on`` surfaces a HIGH/CRITICAL finding). The
    command must be present on PATH or start with ``{python}``; a scanner that
    cannot start is treated as a failed gate (fail-closed), not skipped.
    """
    return await _run_exec(
        settings,
        worktree,
        command_id=scan.id,
        argv=list(scan.argv),
        start_error_prefix="supply-chain scanner could not start",
    )


class VerificationCommitManager:
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
        self._records: dict[str, VerificationRecord] = {}
        self._records_lock = asyncio.Lock()
        # Durable state: re-hydrate verification records from disk so a restart
        # does not invalidate the single-use verification authority a caller
        # may still be holding. Opens a second connection to the same database
        # the jobs manager uses; WAL serializes the two writers safely.
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_records()

    def _reload_records(self) -> None:
        """Load persisted verification records, skipping unparseable rows."""
        for row in store.load_record_rows(self._db):
            record = record_from_row(row)
            if record is not None:
                self._records[record.verification_id] = record

    def _persist_record(self, record: VerificationRecord) -> None:
        """Persist one verification record as a single ACID transaction.

        Call this inside ``async with self._records_lock`` after mutating a
        record so the durable store mirrors the in-memory state.
        """
        store.upsert_record_row(self._db, record_to_row(record))

    def _new_verification_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if candidate not in self._records:
                return candidate
        raise VerificationError("could not allocate a unique verification ID")

    @staticmethod
    def _commands_diverge(first: dict[str, Any], second: dict[str, Any]) -> bool:
        """True when two runs of the same command produced different outcomes.

        Only the determinism-relevant fields are compared: the status, exit
        code, and stderr text. stdout may legitimately differ (e.g. timestamps
        in output), but a divergent status, exit code, or stderr is a real flake.
        """
        return (
            first.get("status") != second.get("status")
            or first.get("exit_code") != second.get("exit_code")
            or _stderr_text(first) != _stderr_text(second)
        )

    async def verify(
        self,
        project: ProjectPolicy,
        job_id: str,
        expected_head: str,
    ) -> dict[str, Any]:
        lock = await self._locks.for_project(project.id)
        async with lock:
            try:
                worktree = await self._jobs.verification_worktree(project, job_id)
                if expected_head != worktree.base_head:
                    raise VerificationError("expected_head does not match the job base HEAD")
                await self._worktrees.validate_resume(project, worktree)
                before = await _candidate_state(self.settings, project, worktree)
                command_results: list[dict[str, Any]] = []
                redacted_paths = (
                    worktree.path,
                    project.root,
                    self.settings.job_worktree_root,
                    self.settings.projects_config_path,
                    *self._project_roots,
                )
                repetitions = max(1, self.settings.verification_deterministic_repetitions)
                nondeterminism_labeled = False
                for command in project.verification_commands:
                    # M-CC-09 flaky detection: run a command's deterministic
                    # subset ``deterministic_repetitions`` times; if the results
                    # diverge, label the verification as nondeterministic and
                    # degrade the infrastructure status.
                    first: dict[str, Any] | None = None
                    for _ in range(repetitions):
                        result = await _run_command(self.settings, worktree, command)
                        bounded = _bound_redacted_output(
                            _redact(result, redacted_paths),
                            self.settings.max_verification_output_bytes,
                        )
                        if first is None:
                            first = bounded
                        elif self._commands_diverge(first, bounded):
                            nondeterminism_labeled = True
                    command_results.append(first)
                results = tuple(command_results)
                after = await _candidate_state(self.settings, project, worktree)
            except (DevelopmentJobError, WorktreeError, GitCommandError) as exc:
                raise VerificationError(str(exc)) from exc

            unchanged = before == after
            passed = unchanged and all(result["status"] == "passed" for result in results)
            response: dict[str, Any] = {
                "status": "passed" if passed else "failed",
                "project_id": project.id,
                "job_id": job_id,
                "base_head": before.base_head,
                "diff_fingerprint": before.fingerprint,
                "paths": list(before.paths),
                "state_unchanged": unchanged,
                "commands": list(results),
                "deterministic_repetitions": repetitions,
                "nondeterminism_labeled": nondeterminism_labeled,
                "infrastructure_status": "DEGRADED"
                if nondeterminism_labeled
                else "HEALTHY",
            }
            if not passed:
                if not unchanged:
                    response["error"] = "verification commands changed the candidate state"
                else:
                    response["error"] = "one or more verification commands failed"
                return response

            # M-CC-09 baseline comparison: report whether the candidate's
            # fingerprint matches the last green verification for this project
            # (a deterministic repeat) or diverges from it (a changed baseline).
            baseline = store.load_last_passed_record(self._db, project.id)
            baseline_matches = (
                baseline is not None
                and baseline.get("diff_fingerprint") == before.fingerprint
            )
            response["baseline_fingerprint"] = (
                baseline.get("diff_fingerprint") if baseline is not None else None
            )
            response["baseline_matches"] = baseline_matches

            record = VerificationRecord(
                verification_id=self._new_verification_id(),
                project_id=project.id,
                job_id=job_id,
                base_head=before.base_head,
                diff_fingerprint=before.fingerprint,
                paths=before.paths,
                verified_at=_timestamp(),
                commands=results,
            )
            async with self._records_lock:
                self._records[record.verification_id] = record
                self._persist_record(record)
            response["verification_id"] = record.verification_id
            response["verified_at"] = record.verified_at
            return response

    async def _record(
        self,
        project: ProjectPolicy,
        job_id: str,
        verification_id: str,
    ) -> VerificationRecord:
        async with self._records_lock:
            record = self._records.get(verification_id)
            if (
                record is None
                or record.project_id != project.id
                or record.job_id != job_id
            ):
                raise VerificationError("unknown verification_id for this project and job")
            if record.used:
                raise VerificationError("verification_id has already been used")
            return record

    async def commit(
        self,
        project: ProjectPolicy,
        job_id: str,
        verification_id: str,
        expected_head: str,
        expected_diff_fingerprint: str,
        paths: list[str],
        message: str,
    ) -> dict[str, Any]:
        commit_message = message.strip()
        if not commit_message:
            raise VerificationError("commit message must not be empty")
        if len(commit_message) > self.settings.max_commit_message_chars:
            raise VerificationError(
                f"commit message exceeds the {self.settings.max_commit_message_chars}-character limit"
            )
        if any(ord(character) < 32 and character not in "\n\t" for character in commit_message):
            raise VerificationError("commit message contains disallowed control characters")

        lock = await self._locks.for_project(project.id)
        async with lock:
            record = await self._record(project, job_id, verification_id)
            if expected_head != record.base_head:
                raise VerificationError("expected_head does not match verified base HEAD")
            if expected_diff_fingerprint != record.diff_fingerprint:
                raise VerificationError(
                    "expected_diff_fingerprint does not match verification evidence"
                )
            if tuple(paths) != record.paths or len(paths) != len(set(paths)):
                raise VerificationError("paths must exactly match the verified ordered path list")

            worktree = await self._jobs.verification_worktree(project, job_id)
            try:
                await self._worktrees.validate_resume(project, worktree)
                current = await _candidate_state(self.settings, project, worktree)
            except (DevelopmentJobError, WorktreeError, GitCommandError) as exc:
                raise VerificationError(str(exc)) from exc
            if current != CandidateState(
                record.base_head, record.diff_fingerprint, record.paths
            ):
                # M-CT-05: provenance corruption — the candidate fingerprint no
                # longer matches the verified state. Quarantine the job (terminal,
                # non-resumable) before raising so the corruption is durably
                # recorded, not silently lost in an exception.
                await self._jobs.quarantine_provenance(
                    project, job_id, "candidate state changed after verification"
                )
                raise VerificationError("candidate state changed after verification")

            # M-CC-11: severity-gated supply-chain gate. If the project opts in
            # with supply_chain_scans, every scanner must exit 0 (e.g. grype
            # --fail-on=high, pip-audit) or the commit is rejected before any
            # staging. Each result is appended to the supply_chain_scans audit
            # table. Runs inside the candidate worktree (isolated), fixed-argv,
            # fail-closed on missing scanners.
            supply_chain_results: list[dict[str, Any]] = []
            if project.supply_chain_scans:
                redacted_scan_paths = (
                    worktree.path,
                    project.root,
                    self.settings.job_worktree_root,
                    self.settings.projects_config_path,
                    *self._project_roots,
                )
                for scan in project.supply_chain_scans:
                    scan_result = await _run_supply_chain_scan(
                        self.settings, worktree, scan
                    )
                    bounded_scan = _bound_redacted_output(
                        _redact(scan_result, redacted_scan_paths),
                        self.settings.max_verification_output_bytes,
                    )
                    supply_chain_results.append(bounded_scan)
                    store.insert_supply_chain_scan(
                        self._db,
                        {
                            "project_id": project.id,
                            "job_id": job_id,
                            "verification_id": verification_id,
                            "scan_command": scan.id,
                            "scan_argv": _json_dumps(list(scan.argv)),
                            "exit_code": bounded_scan.get("exit_code"),
                            "passed": 1
                            if bounded_scan.get("status") == "passed"
                            else 0,
                            "scanned_at": _timestamp(),
                            "detail_json": _json_dumps(
                                {
                                    "status": bounded_scan.get("status"),
                                    "stderr": _stderr_text(bounded_scan)[-1000:],
                                }
                            ),
                        },
                    )
                failed_scans = [
                    result.get("command_id")
                    for result in supply_chain_results
                    if result.get("status") != "passed"
                ]
                if failed_scans:
                    raise VerificationError(
                        "supply-chain gate rejected the commit: scanners failed "
                        f"({', '.join(str(failed_scans))})"
                    )

            timeout = self.settings.git_command_timeout_seconds
            staged = False
            try:
                await run_git(
                    worktree.path,
                    "add",
                    "--",
                    *record.paths,
                    timeout_seconds=timeout,
                )
                staged = True
                staged_paths = _decode_paths(
                    await _git_bytes(
                        worktree.path,
                        "diff",
                        "--cached",
                        "--name-only",
                        "-z",
                        "--no-renames",
                        "HEAD",
                        "--",
                        timeout_seconds=timeout,
                    )
                )
                if tuple(sorted(staged_paths)) != record.paths:
                    raise VerificationError("Git staged a path set different from verification")
                unstaged = await _git_bytes(
                    worktree.path,
                    "diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    "--",
                    timeout_seconds=timeout,
                )
                untracked = await _git_bytes(
                    worktree.path,
                    "ls-files",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                    timeout_seconds=timeout,
                )
                if unstaged or untracked:
                    raise VerificationError("candidate changed while exact paths were staged")
                staged_state = await _staged_state(
                    self.settings, project, worktree, record.paths
                )
                if staged_state != CandidateState(
                    record.base_head, record.diff_fingerprint, record.paths
                ):
                    # M-CT-05: provenance corruption — the staged content does not
                    # re-derive the verified candidate state. Quarantine the job
                    # (terminal, non-resumable) before raising.
                    await self._jobs.quarantine_provenance(
                        project,
                        job_id,
                        "staged content does not match the verified candidate state",
                    )
                    raise VerificationError(
                        "staged content does not match the verified candidate state"
                    )

                commit_sha = await run_git(
                    worktree.path,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--no-verify",
                    "--message",
                    commit_message,
                    timeout_seconds=timeout,
                )
                del commit_sha  # Git commit stdout is not authoritative evidence.
                new_head = await run_git(
                    worktree.path,
                    "rev-parse",
                    "--verify",
                    "HEAD",
                    timeout_seconds=timeout,
                )
                parent = await run_git(
                    worktree.path,
                    "rev-parse",
                    "--verify",
                    "HEAD^",
                    timeout_seconds=timeout,
                )
                committed_paths = _decode_paths(
                    await _git_bytes(
                        worktree.path,
                        "diff-tree",
                        "--no-commit-id",
                        "--name-only",
                        "-r",
                        "-z",
                        "--no-renames",
                        "HEAD",
                        "--",
                        timeout_seconds=timeout,
                    )
                )
                status = await run_git(
                    worktree.path,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    timeout_seconds=timeout,
                )
                branch = await run_git(
                    worktree.path,
                    "branch",
                    "--show-current",
                    timeout_seconds=timeout,
                )
                if parent != record.base_head:
                    raise VerificationError("detached commit has an unexpected parent")
                if tuple(sorted(committed_paths)) != record.paths:
                    raise VerificationError("detached commit contains unexpected paths")
                if status or branch:
                    raise VerificationError("detached commit did not leave a clean worktree")
            except (GitCommandError, WorktreeError) as exc:
                if staged:
                    try:
                        await run_git(
                            worktree.path,
                            "reset",
                            "--mixed",
                            "HEAD",
                            "--",
                            timeout_seconds=timeout,
                        )
                    except GitCommandError:
                        pass
                raise VerificationError(str(exc)) from exc
            except Exception:
                if staged:
                    try:
                        await run_git(
                            worktree.path,
                            "reset",
                            "--mixed",
                            "HEAD",
                            "--",
                            timeout_seconds=timeout,
                        )
                    except GitCommandError:
                        pass
                raise

            async with self._records_lock:
                record.used = True
                self._persist_record(record)
            await self._jobs.record_commit(project, job_id, new_head)
            return {
                "status": "committed",
                "project_id": project.id,
                "job_id": job_id,
                "verification_id": verification_id,
                "commit_sha": new_head,
                "parent_head": parent,
                "paths": list(record.paths),
                "message": commit_message,
                "detached": True,
                "published": False,
            }


class PublicationError(RuntimeError):
    """Raised when a detached development commit cannot be safely published."""


_MAX_BRANCH_LENGTH = 200
_FORBIDDEN_BRANCH_NAMES = frozenset({"main", "master", "head"})
_BRANCH_FORBIDDEN_CHARS = frozenset(" \t\n~^:?*[\\")


def _validate_branch_name(branch_name: str) -> None:
    """Reject anything that is not a safe, non-trunk short branch name.

    The caller supplies the short name only; the bridge forms
    ``refs/heads/<branch_name>``. This guard refuses full refs, ``HEAD``, the
    trunk branches (``main``/``master`` and the repo default, checked later),
    leading/trailing or consecutive slashes, leading dots, ``..``, ``.lock``
    suffixes, ``@{``/``@``, and any control or git-refname-forbidden characters.
    """
    if not branch_name or len(branch_name) > _MAX_BRANCH_LENGTH:
        raise PublicationError("branch_name must be between 1 and 200 characters")
    if branch_name.startswith("refs/"):
        raise PublicationError("branch_name must be a short branch name, not a full ref")
    if branch_name == "HEAD" or branch_name.startswith("HEAD/"):
        raise PublicationError("branch_name must not be or start with 'HEAD'")
    if branch_name.startswith("/") or branch_name.endswith("/"):
        raise PublicationError("branch_name must not start or end with '/'")
    if branch_name == "@" or "@{" in branch_name:
        raise PublicationError("branch_name must not be '@' or contain '@{'")
    parts = branch_name.split("/")
    for part in parts:
        if not part:
            raise PublicationError("branch_name must not contain consecutive '/' segments")
        if part.startswith("."):
            raise PublicationError("branch_name components must not start with '.'")
        if part.endswith(".lock"):
            raise PublicationError("branch_name components must not end with '.lock'")
        if ".." in part:
            raise PublicationError("branch_name must not contain '..'")
        for character in part:
            if character in _BRANCH_FORBIDDEN_CHARS or ord(character) < 32:
                raise PublicationError("branch_name contains forbidden characters")
    if parts[-1].lower() in _FORBIDDEN_BRANCH_NAMES:
        raise PublicationError(
            "branch_name must not target a protected trunk branch (main, master, or HEAD)"
        )


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    project_id: str
    job_id: str
    commit_sha: str
    branch_name: str
    remote_url: str
    published_at: str


def publication_to_row(record: PublicationRecord) -> dict[str, Any]:
    return {
        "project_id": record.project_id,
        "job_id": record.job_id,
        "commit_sha": record.commit_sha,
        "branch_name": record.branch_name,
        "remote_url": record.remote_url,
        "published_at": record.published_at,
    }


def publication_from_row(row: dict[str, Any]) -> PublicationRecord | None:
    try:
        return PublicationRecord(
            project_id=row["project_id"],
            job_id=row["job_id"],
            commit_sha=row["commit_sha"],
            branch_name=row["branch_name"],
            remote_url=row["remote_url"],
            published_at=row["published_at"],
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _remote_branch_sha(
    root: Path,
    branch_name: str,
    *,
    timeout_seconds: int,
) -> str | None:
    """Return the SHA ``refs/heads/<branch_name>`` points at on origin, or None."""
    payload = await _git_bytes(
        root,
        "ls-remote",
        "--heads",
        "origin",
        branch_name,
        timeout_seconds=timeout_seconds,
    )
    payload = payload.strip()
    if not payload:
        return None
    sha, _, _ = payload.partition(b"\t")
    return sha.decode("ascii")


async def _remote_default_branch(root: Path, *, timeout_seconds: int) -> str | None:
    """Best-effort origin default branch name, or None if it cannot be resolved.

    ``git symbolic-ref refs/remotes/origin/HEAD`` yields the full target
    (``refs/remotes/origin/<name>``); ``--short`` would yield ``origin/<name>``,
    so the full form is parsed and the ``refs/remotes/origin/`` prefix stripped.
    """
    try:
        full = await run_git(
            root,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            timeout_seconds=timeout_seconds,
        )
    except GitCommandError:
        return None
    prefix = "refs/remotes/origin/"
    return full[len(prefix):] if full.startswith(prefix) else None


def _paths_from_verification_row(row: dict[str, Any]) -> tuple[str, ...]:
    try:
        loaded = json.loads(row["paths_json"])
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise PublicationError("verification record paths are corrupt") from exc
    if not isinstance(loaded, list) or not loaded:
        raise PublicationError("verification record has no paths")
    if not all(isinstance(item, str) and item for item in loaded):
        raise PublicationError("verification record paths are invalid")
    return tuple(loaded)


class PublicationManager:
    """Publishes a verified detached development commit to a new remote branch.

    The manager owns the additive ``publications`` table in ``bridge.db`` (one
    row per ``(project_id, job_id)``) and re-hydrates it synchronously in
    ``__init__`` so a restart preserves the single-branch-per-job bound.
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
        self._project_roots = tuple(project.root for project in projects)
        self._publications: dict[tuple[str, str], PublicationRecord] = {}
        self._publications_lock = asyncio.Lock()
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_publications()

    def _reload_publications(self) -> None:
        for row in store.load_publication_rows(self._db):
            record = publication_from_row(row)
            if record is not None:
                self._publications[(record.project_id, record.job_id)] = record

    def _persist_publication(self, record: PublicationRecord) -> None:
        store.upsert_publication_row(self._db, publication_to_row(record))

    def _redaction_paths(
        self, project: ProjectPolicy, worktree: JobWorktree
    ) -> tuple[Path, ...]:
        return (
            worktree.path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def _push_and_verify(
        self,
        worktree: JobWorktree,
        commit_sha: str,
        branch_name: str,
        *,
        timeout: int,
    ) -> None:
        await run_git(
            worktree.path,
            "-c",
            "core.hooksPath=/dev/null",
            "push",
            "origin",
            f"{commit_sha}:refs/heads/{branch_name}",
            timeout_seconds=timeout,
        )
        remote_sha = await _remote_branch_sha(
            worktree.path, branch_name, timeout_seconds=timeout
        )
        if remote_sha != commit_sha:
            raise PublicationError(
                "push did not create a remote branch pointing at the requested commit"
            )

    def _response(
        self,
        project: ProjectPolicy,
        job_id: str,
        commit_sha: str,
        branch_name: str,
        remote_url: str,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "status": "published",
            "project_id": project.id,
            "job_id": job_id,
            "commit_sha": commit_sha,
            "branch_name": branch_name,
            "remote_ref": f"refs/heads/{branch_name}",
            "remote_url": remote_url,
            "published": True,
            "idempotent": idempotent,
        }

    async def publish(
        self,
        project: ProjectPolicy,
        job_id: str,
        commit_sha: str,
        branch_name: str,
        expected_base_head: str,
        expected_diff_fingerprint: str,
    ) -> dict[str, Any]:
        commit_sha = commit_sha.lower()
        expected_base_head = expected_base_head.lower()
        expected_diff_fingerprint = expected_diff_fingerprint.lower()
        _validate_branch_name(branch_name)

        lock = await self._locks.for_project(project.id)
        async with lock:
            try:
                candidate = await self._jobs.committed_candidate(project, job_id)
            except DevelopmentJobError as exc:
                raise PublicationError(str(exc)) from exc
            if candidate.committed_sha != commit_sha:
                raise PublicationError(
                    "commit_sha does not match the detached commit created for this job"
                )
            worktree = candidate.worktree
            if expected_base_head != worktree.base_head:
                raise PublicationError("expected_base_head does not match the job base HEAD")
            timeout = self.settings.git_command_timeout_seconds

            # Authorization: a used verification record for this exact evidence.
            record_row = store.used_verification_record(
                self._db,
                project.id,
                job_id,
                expected_base_head,
                expected_diff_fingerprint,
            )
            if record_row is None:
                raise PublicationError(
                    "no matching used verification record for this base HEAD and fingerprint"
                )
            paths = _paths_from_verification_row(record_row)

            try:
                resolved = await run_git(
                    worktree.path,
                    "rev-parse",
                    "--verify",
                    f"{commit_sha}^{{commit}}",
                    timeout_seconds=timeout,
                )
                if resolved != commit_sha:
                    raise PublicationError("commit_sha did not resolve to itself")
                parent = await run_git(
                    worktree.path,
                    "rev-parse",
                    "--verify",
                    f"{commit_sha}^",
                    timeout_seconds=timeout,
                )
                if parent != expected_base_head:
                    raise PublicationError(
                        "commit_sha parent does not match the verified base HEAD"
                    )
                committed_paths = tuple(
                    sorted(
                        _decode_paths(
                            await _git_bytes(
                                worktree.path,
                                "diff-tree",
                                "--no-commit-id",
                                "--name-only",
                                "-r",
                                "-z",
                                "--no-renames",
                                commit_sha,
                                "--",
                                timeout_seconds=timeout,
                            )
                        )
                    )
                )
                if committed_paths != paths:
                    # M-CT-05: provenance corruption — the committed path set
                    # differs from the verification record. Quarantine the job
                    # (terminal, non-resumable) before raising.
                    await self._jobs.quarantine_provenance(
                        project,
                        job_id,
                        "commit changes a different path set than the verification record",
                    )
                    raise PublicationError(
                        "commit changes a different path set than the verification record"
                    )
                rederived = await committed_state(
                    self.settings,
                    project,
                    worktree,
                    commit_sha,
                    expected_base_head,
                    paths,
                )
                if rederived.fingerprint != expected_diff_fingerprint:
                    # M-CT-05: provenance corruption — the committed state does
                    # not re-derive the verified candidate fingerprint. Quarantine
                    # the job (terminal, non-resumable) before raising.
                    await self._jobs.quarantine_provenance(
                        project,
                        job_id,
                        "commit_sha does not realize the verified candidate fingerprint",
                    )
                    raise PublicationError(
                        "commit_sha does not realize the verified candidate fingerprint"
                    )
            except (GitCommandError, VerificationError, WorktreeError) as exc:
                raise PublicationError(str(exc)) from exc

            try:
                remote_url = await run_git(
                    worktree.path,
                    "remote",
                    "get-url",
                    "origin",
                    timeout_seconds=timeout,
                )
            except GitCommandError as exc:
                raise PublicationError(
                    "origin remote is not configured for this project"
                ) from exc

            default_branch = await _remote_default_branch(
                worktree.path, timeout_seconds=timeout
            )
            if default_branch is not None and branch_name == default_branch:
                raise PublicationError(
                    "branch_name must not target the repository default branch"
                )

            async with self._publications_lock:
                existing = self._publications.get((project.id, job_id))
            if existing is not None:
                if (
                    existing.commit_sha != commit_sha
                    or existing.branch_name != branch_name
                ):
                    raise PublicationError(
                        "development job has already been published to a different branch"
                    )
                remote_sha = await _remote_branch_sha(
                    worktree.path, branch_name, timeout_seconds=timeout
                )
                if remote_sha == commit_sha:
                    return _redact(
                        self._response(
                            project,
                            job_id,
                            commit_sha,
                            branch_name,
                            remote_url,
                            idempotent=True,
                        ),
                        self._redaction_paths(project, worktree),
                    )
                if remote_sha is not None:
                    raise PublicationError(
                        "remote branch no longer points at the published commit"
                    )
                # The remote branch vanished since the prior publish: re-push to
                # recover the published ref. The branch is absent, so the new-branch
                # pre-check below does not apply.
                try:
                    await self._push_and_verify(
                        worktree, commit_sha, branch_name, timeout=timeout
                    )
                except (GitCommandError, VerificationError) as exc:
                    raise PublicationError(str(exc)) from exc
                return _redact(
                    self._response(
                        project,
                        job_id,
                        commit_sha,
                        branch_name,
                        remote_url,
                        idempotent=False,
                    ),
                    self._redaction_paths(project, worktree),
                )

            # No prior publication: the remote branch must not already exist.
            remote_sha = await _remote_branch_sha(
                worktree.path, branch_name, timeout_seconds=timeout
            )
            if remote_sha is not None:
                raise PublicationError(
                    "remote branch already exists; publication requires a new branch"
                )
            try:
                await self._push_and_verify(
                    worktree, commit_sha, branch_name, timeout=timeout
                )
            except (GitCommandError, VerificationError) as exc:
                raise PublicationError(str(exc)) from exc
            record = PublicationRecord(
                project_id=project.id,
                job_id=job_id,
                commit_sha=commit_sha,
                branch_name=branch_name,
                remote_url=remote_url,
                published_at=_timestamp(),
            )
            async with self._publications_lock:
                self._publications[(project.id, job_id)] = record
                self._persist_publication(record)
            return _redact(
                self._response(
                    project,
                    job_id,
                    commit_sha,
                    branch_name,
                    remote_url,
                    idempotent=False,
                ),
                self._redaction_paths(project, worktree),
            )
