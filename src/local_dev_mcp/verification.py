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
from .models import ProjectPolicy, VerificationCommand
from .registry import ProjectLockManager
from .worktrees import GitWorktreeManager, JobWorktree, WorktreeError


class VerificationError(RuntimeError):
    """Raised when a candidate cannot be safely verified or committed."""


MAX_COMMIT_PATHS = 500
_FINGERPRINT_MARKER = b"local-dev-mcp-candidate-state-v1\0"


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
    descriptor = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
    )
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

    digest = hashlib.sha256()
    digest.update(_FINGERPRINT_MARKER)
    digest.update(_frame(worktree.base_head.encode("ascii")))
    for relative in paths:
        _validate_policy_path(project, relative)
        current = _safe_path(worktree.path, relative, allow_missing=True)
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
        if current is None:
            digest.update(_frame(b"deleted"))
            continue
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(current, flags)
        except OSError as exc:
            raise VerificationError(f"candidate file could not be opened safely: {exc}") from exc
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_descriptor)
            raise VerificationError("changed paths must be regular files or deletions")
        git_mode = b"100755" if metadata.st_mode & 0o111 else b"100644"
        digest.update(_frame(git_mode))
        digest.update(_frame(str(metadata.st_size).encode("ascii")))
        content_hash = hashlib.sha256()
        actual_size = 0
        with os.fdopen(file_descriptor, "rb") as handle:
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
            raise VerificationError("candidate file changed while it was fingerprinted")
        digest.update(_frame(content_hash.hexdigest().encode("ascii")))
    return CandidateState(
        base_head=worktree.base_head,
        fingerprint=digest.hexdigest(),
        paths=paths,
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


async def _run_command(
    settings: Settings,
    worktree: JobWorktree,
    command: VerificationCommand,
) -> dict[str, Any]:
    expanded = list(command.argv)
    if expanded[0] == "{python}":
        expanded[0] = sys.executable
    environment = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    python_path = _python_paths(worktree.path, command.python_path)
    if python_path is not None:
        environment["PYTHONPATH"] = python_path

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
            "command_id": command.id,
            "status": "failed",
            "exit_code": None,
            "error": f"verification executable could not start: {exc}",
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
        "command_id": command.id,
        "status": "timed_out"
        if timed_out
        else ("passed" if process.returncode == 0 else "failed"),
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


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
                for command in project.verification_commands:
                    result = await _run_command(self.settings, worktree, command)
                    command_results.append(
                        _bound_redacted_output(
                            _redact(result, redacted_paths),
                            self.settings.max_verification_output_bytes,
                        )
                    )
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
            }
            if not passed:
                if not unchanged:
                    response["error"] = "verification commands changed the candidate state"
                else:
                    response["error"] = "one or more verification commands failed"
                return response

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
                raise VerificationError("candidate state changed after verification")

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
