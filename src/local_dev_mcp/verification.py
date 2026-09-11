from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import platform
import re
import secrets
import signal
import stat
import sys
import tempfile
import tomllib
from xml.etree import ElementTree
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import store
from .config import Settings
from .git import GitCommandError, run_git
from .jobs import DevelopmentJobError, DevelopmentJobManager
from .models import (
    ProjectPolicy,
    StaticAnalysis,
    StaticSchemaCheck,
    SupplyChainCommand,
    TestKind,
    VerificationCommand,
)
from .registry import ProjectLockManager
from .worktrees import GitWorktreeManager, JobWorktree, WorktreeError


class VerificationError(RuntimeError):
    """Raised when a candidate cannot be safely verified or committed."""


MAX_COMMIT_PATHS = 500
_FINGERPRINT_MARKER = b"local-dev-mcp-candidate-state-v1\0"

# M-CC-10: candidate-corruption signatures. When ``_candidate_state`` (or a
# helper it calls) raises one of these, the candidate cannot be re-derived
# because the worktree state is inconsistent — a staged index, a path that
# changed/vanished while it was read, an unresolved index entry, an unexpected
# path type, or a path that Git cannot fingerprint. These are distinct from
# benign rejections (an empty changed set, a policy-blocked path): corruption
# triggers the M-CC-10 rollback to the last verified checkpoint; a benign
# rejection merely fails the verification.
_CANDIDATE_CORRUPTION_SIGNATURES: tuple[str, ...] = (
    "candidate index must be empty before verification",
    "candidate paths must be valid UTF-8",
    "candidate index contains an unresolved entry",
    "candidate index contains an unexpected path type",
    "candidate path no longer exists",
    "candidate worktree could not be opened",
    "candidate paths must not traverse symbolic links or non-directories",
    "changed symbolic links are not allowed",
    "changed symbolic links and submodules are not allowed",
    "candidate paths must be regular files or deletions",
    "candidate path could not be inspected",
    "Git returned an invalid staged blob size",
    "Git could not read a staged blob",
    "Git staged blob size changed while it was read",
)


def _is_candidate_corruption(error: VerificationError) -> bool:
    """True when a VerificationError is a candidate-corruption signature."""
    message = str(error)
    return any(signature in message for signature in _CANDIDATE_CORRUPTION_SIGNATURES)

# M-EV-02: the allowlisted env keys used by ``_run_exec`` that form the
# deterministic verification environment. Only their *set* values (constants
# pinned by M-CC-09, not the real process env) are part of the digest, so the
# digest is stable across runs with the same declared environment. Kept in a
# stable sorted definition order so the digest is deterministic.
_ENV_DIGEST_KEYS: tuple[str, ...] = (
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "PYTHONHASHSEED",
    "SOURCE_DATE_EPOCH",
    "TZ",
)

# The fixed values ``_run_exec`` sets for the deterministic env (M-CC-09). These
# are constants, so the environment digest is stable across runs/machines.
_DETERMINISTIC_ENV: dict[str, str] = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONHASHSEED": "0",
    "SOURCE_DATE_EPOCH": "0",
    "TZ": "UTC",
}


def _command_digest(command: VerificationCommand) -> str:
    """SHA-256 over a command's declared id + argv (M-EV-02).

    The digest is over the *declared* argv (the fixed ``VerificationCommand``
    vector), not the expanded argv (``{python}`` substitution, sandbox
    wrapping, PYTHONPATH injection), so it is stable across runs and does not
    leak absolute paths. A changed argv or id therefore changes the digest.
    The test ``kind`` (M-ROADMAP) folds in only when non-unit, so unit
    digests stay byte-identical to pre-kind records.
    """
    digest = hashlib.sha256()
    digest.update(_frame(command.id.encode("utf-8")))
    for argument in command.argv:
        digest.update(_frame(argument.encode("utf-8")))
    if command.kind is not TestKind.UNIT:
        digest.update(_frame(command.kind.value.encode("utf-8")))
    return digest.hexdigest()


def compute_environment_digest(
    commands: tuple[VerificationCommand, ...],
    *,
    toolchain_pin_digest: str | None = None,
) -> str:
    """SHA-256 over the stable, bounded verification environment tuple.

    M-EV-02: covers the Python interpreter version
    (``platform.python_version()``), the allowlisted deterministic env keys
    used by ``_run_exec`` (their set values, not the real process env), each
    verification command's id + argv (the declared fixed vector), and
    (optionally, if configured) a toolchain-pin file digest. Deterministic:
    keys are in a fixed sorted order, volatile values (timestamps, PIDs,
    absolute paths that vary per run) are excluded, and the argv is the
    declared vector rather than the expanded one.
    """
    digest = hashlib.sha256()
    digest.update(b"local-dev-mcp-env-digest-v1\0")
    digest.update(_frame(platform.python_version().encode("ascii")))
    for key in _ENV_DIGEST_KEYS:
        digest.update(_frame(key.encode("ascii")))
        digest.update(_frame(_DETERMINISTIC_ENV[key].encode("utf-8")))
    for command in commands:
        digest.update(_frame(command.id.encode("utf-8")))
        for argument in command.argv:
            digest.update(_frame(argument.encode("utf-8")))
        if command.kind is not TestKind.UNIT:
            digest.update(_frame(command.kind.value.encode("utf-8")))
    if toolchain_pin_digest is not None:
        digest.update(_frame(b"toolchain"))
        digest.update(_frame(toolchain_pin_digest.encode("ascii")))
    return digest.hexdigest()


def _toolchain_python(pin: str) -> str:
    """Resolve the pinned interpreter for a toolchain pin (M-CC-09).

    A pin may name either a venv directory (whose interpreter lives at
    ``<pin>/bin/python``) or an interpreter/executable file directly. The
    resolved path replaces ``{python}`` when the project opts into a pinned
    toolchain, so verification commands run against the operator-pinned
    interpreter instead of the bridge's own.
    """
    path = Path(pin)
    if path.is_dir():
        return str(path / "bin" / "python")
    return pin


def _toolchain_pin_digest(pin: str) -> str:
    """SHA-256 over the canonical toolchain-pin path (M-CC-09).

    The pin is already canonicalized (absolute, resolved) by the model
    validator, so the digest is stable across runs and changes only when the
    operator changes the pin. It feeds ``compute_environment_digest`` so a
    pinned-toolchain verification carries a stable toolchain fingerprint.
    """
    digest = hashlib.sha256()
    digest.update(b"local-dev-mcp-toolchain-pin-v1\0")
    digest.update(_frame(os.path.abspath(pin).encode("utf-8")))
    return digest.hexdigest()


def _network_failed(command_id: str, error: str) -> dict[str, Any]:
    """A fail-closed verification result for a network-isolation violation.

    M-CC-09: when a project's network policy requires isolation the sandbox
    cannot deliver for this command, the command must not run unsandboxed —
    it returns a failed result (exit code None, no authority), which fails the
    whole verification closed.
    """
    return {
        "command_id": command_id,
        "status": "failed",
        "exit_code": None,
        "error": error,
    }


def _write_network_hosts_file(allowlist: tuple[str, ...]) -> str:
    """Write the allowlist hosts file for a network-policy sandbox (M-CC-09).

    The generated file pins every host to loopback: ``localhost`` plus each
    allowlisted hostname (reachable only via the project's loopback proxy).
    Combined with an empty ``/etc/resolv.conf`` bound into the sandbox, DNS
    resolution for any host NOT in the allowlist fails closed, so a sandboxed
    command can only ever reach the allowlisted loopback endpoints. Returns the
    path of the generated file (the caller removes it after the run).
    """
    descriptor, path = tempfile.mkstemp(prefix="bridge-hosts-", suffix=".conf")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("127.0.0.1 localhost\n")
            for host in allowlist:
                handle.write(f"127.0.0.1 {host}\n")
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _write_empty_resolv_conf() -> str:
    """Write an empty resolv.conf for a network-policy sandbox (M-CC-09).

    Binding this at ``/etc/resolv.conf`` inside the sandbox makes every DNS
    query fail, so only hostnames present in the allowlist hosts file (pinned
    to loopback) are reachable — everything else fails closed.
    """
    descriptor, path = tempfile.mkstemp(prefix="bridge-resolv-", suffix=".conf")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8"):
            pass
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _stderr_text(result: dict[str, Any]) -> str:
    """Extract the bounded stderr text from a command result dict (or '')."""
    stderr = result.get("stderr")
    if isinstance(stderr, dict):
        text = stderr.get("text")
        return text if isinstance(text, str) else ""
    return stderr if isinstance(stderr, str) else ""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """True when an ISO-8601 ``expires_at`` has passed (M-EV-03).

    A NULL/absent ``expires_at`` means no expiry → never expired (the gate
    defaults to permissive when expiry is absent). The comparison is against
    the current UTC time. A malformed timestamp is treated as expired so a
    corrupt binding fails closed rather than silently passing the gate.
    """
    if expires_at is None:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        # A naive timestamp is interpreted as UTC.
        parsed = parsed.replace(tzinfo=UTC)
    current = now if now is not None else datetime.now(UTC)
    return parsed < current


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


_GLOB_MAGIC = frozenset("*?[")


def _matches_unexpected_pattern(pattern: str, path: str) -> bool:
    """Match a candidate path against one ``unexpected_paths`` entry (M-CC-06).

    A trailing ``/`` makes the entry a directory prefix (``dist/`` matches
    ``dist`` and everything beneath it). A literal entry with no glob magic
    matches the exact path or anything beneath it as a directory (``node_modules``
    matches ``node_modules`` and ``node_modules/...``). Any other entry is
    treated as a shell-style glob on the whole relative path (``*.lock`` matches
    top-level ``.lock`` files only; use ``**/*.lock`` for nested ones).
    """
    if pattern.endswith("/"):
        directory = pattern[:-1]
        return path == directory or path.startswith(directory + "/")
    if not any(char in pattern for char in _GLOB_MAGIC):
        return path == pattern or path.startswith(pattern + "/")
    return fnmatch.fnmatchcase(path, pattern)


def _undeclared_unexpected_paths(
    project: ProjectPolicy,
    changed_paths: tuple[str, ...],
    declared_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Changed paths matching ``unexpected_paths`` that are not declared (M-CC-06).

    Fail-closed only when the policy declares patterns: an empty
    ``unexpected_paths`` tuple never rejects. ``declared_paths`` is the
    verification record's ordered path list — the exact set the verifier
    examined and any reviewer signed off on. A path that matches a declared
    unexpected pattern but IS in the declared set is allowed (it was
    verified); one that appears outside the declared set is flagged.
    """
    if not project.unexpected_paths:
        return ()
    declared = set(declared_paths)
    return tuple(
        sorted(
            path
            for path in changed_paths
            if path not in declared
            and any(
                _matches_unexpected_pattern(pattern, path)
                for pattern in project.unexpected_paths
            )
        )
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
    # M-ROADMAP: advisory static-analysis results (declared analyzers + schema
    # checks) that accompanied this verification, persisted in the
    # ``verification_static_analysis`` sidecar (not on ``verifications``) so
    # the schema stays additive. Empty tuple when the project declares none.
    static_analysis: tuple[dict[str, Any], ...] = ()
    # M-EV-02: SHA-256 over the Python interpreter version + the allowlisted
    # deterministic env keys + the verification command argv + an optional
    # toolchain-pin digest. Persisted in the ``verification_environment``
    # sidecar (not on ``verifications``) so the schema stays additive.
    environment_digest: str | None = None
    # M-EV-03: evidence-binding fields, persisted in the
    # ``verification_bindings`` sidecar (not on ``verifications``) so the
    # schema stays additive. ``toolchain_digest`` in the sidecar reuses this
    # record's ``environment_digest``; ``reviewer_independence_class`` is always
    # NULL for a verification binding (only reviews carry one). ``expires_at``
    # is an ISO-8601 timestamp (NULL means no expiry → always allowed at the
    # commit gate). ``authority_ref`` links to the ``authority_records`` from
    # M-EV-05.
    expires_at: str | None = None
    authority_ref: str | None = None


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


def record_from_row(
    row: dict[str, Any],
    *,
    environment_digest: str | None = None,
    expires_at: str | None = None,
    authority_ref: str | None = None,
) -> VerificationRecord | None:
    """Rebuild a verification record from a dict row, or None if unparseable.

    M-EV-02: the optional ``environment_digest`` comes from the
    ``verification_environment`` sidecar (loaded separately by the caller) so
    the record carries the environment/toolchain fingerprint without the
    ``verifications`` table being altered.

    M-EV-03: ``expires_at`` and ``authority_ref`` come from the
    ``verification_bindings`` sidecar (loaded separately by the caller) so the
    record carries its evidence-binding envelope without ``verifications``
    being altered. The sidecar's ``toolchain_digest`` reuses the
    ``environment_digest`` and is not carried separately on the record.
    """
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
            environment_digest=environment_digest,
            expires_at=expires_at,
            authority_ref=authority_ref,
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
    credential_names: tuple[str, ...] = (),
    start_error_prefix: str = "executable could not start",
) -> dict[str, Any]:
    """Run a fixed-argv subprocess in the candidate worktree and bound its output.

    Shared by the verification commands and the M-CC-11 supply-chain scans.
    The environment is deterministic (M-CC-09) and stripped of tokens/certs; no
    shell is involved. ``credential_names`` (M-CC-11 credential_policy) are the
    ONLY credential env names projected from the bridge's own process
    environment into the subprocess — least privilege, never a full copy. A
    non-zero exit code is reported to the caller, which decides whether it is a
    gate rejection.
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
    if credential_names:
        environment.update(
            {
                name: os.environ[name]
                for name in credential_names
                if name in os.environ
            }
        )

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


def _sandbox_argv(
    worktree: JobWorktree,
    argv: list[str],
    *,
    hosts_file: str | None = None,
    resolv_conf: str | None = None,
    deps_cache_dir: str | None = None,
) -> list[str]:
    """Wrap a fixed-argv command in a bubblewrap (bwrap) syscall sandbox.

    M-CC-11: repo-controlled verification scripts run under a lightweight
    sandbox rather than with the bridge's privilege. The bridge invokes the
    sandbox runner (bwrap) with a fixed argument vector — never the sandbox
    script directly — with ``--unshare-net`` (network none, unless an allowlist
    hosts file is supplied), a read-only root bind, and a frozen clock
    (``SOURCE_DATE_EPOCH``). The candidate worktree is bound read-only so a
    repo-controlled script cannot modify the candidate state it is supposed to
    verify. ``--die-with-parent`` ties the sandbox's lifetime to the bridge so a
    killed bridge cannot orphan a sandboxed process.

    Only the candidate worktree and the minimal interpreter runtime (``/usr``,
    ``/lib``, ``/lib64``, ``/bin``, ``/etc/alternatives``) are bound read-only —
    never the whole host root — so a malicious repo-controlled script cannot
    read host secrets (``/etc/shadow``, ``.env``, project config, ``/home``).
    ``/dev/null`` is the only device exposed.

    M-CC-09 hermetic-lab options (opt-in per project):
    - ``deps_cache_dir``: bound read-only so a command may read the cached deps
      but can never write to them.
    - ``hosts_file`` (allowlist mode): an allowlist hosts file bound read-only at
      ``/etc/hosts`` together with ``resolv_conf`` (an empty file bound at
      ``/etc/resolv.conf``), so DNS for any non-allowlisted host fails closed.
      In this mode ``--unshare-net`` is omitted (the sandbox needs a real
      network namespace to reach the allowlisted loopback proxy). The caller
      owns both temp files and removes them after the run.
    """
    arguments: list[str] = [
        "bwrap",
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
    ]
    if hosts_file is None:
        # Default sandbox network policy: no network namespace.
        arguments.append("--unshare-net")
    else:
        # Allowlist mode: pin DNS to the allowlist hosts file + empty resolv.conf.
        arguments.extend(
            [
                "--ro-bind",
                hosts_file,
                "/etc/hosts",
                "--ro-bind",
                resolv_conf,
                "/etc/resolv.conf",
            ]
        )
    if deps_cache_dir is not None:
        arguments.extend(["--ro-bind", deps_cache_dir, deps_cache_dir])
    arguments.extend(
        [
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
    )
    return arguments


def _credential_names(project: ProjectPolicy) -> tuple[str, ...]:
    """The credential env names a project declares (empty when unset)."""
    return (
        project.credential_policy.allow
        if project.credential_policy is not None
        else ()
    )


def _parse_mutation_stats(result: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort parse of mutation-testing output (M-ROADMAP test-kind).

    Recognises the conventional mutmut ``Killed`` / ``Survived`` summary lines
    and a ``Percentage`` coverage line in stdout or stderr. Returns
    ``{"killed": int, "survived": int, "kill_ratio": float}`` when parseable,
    else ``None`` — mutation coverage is advisory, never fatal.
    """
    texts: list[str] = []
    for stream in (result.get("stdout"), result.get("stderr")):
        if isinstance(stream, str):
            texts.append(stream)
        elif isinstance(stream, dict):
            text = stream.get("text")
            if isinstance(text, str):
                texts.append(text)
    output = "\n".join(texts)
    killed: int | None = None
    survived: int | None = None
    kill_ratio: float | None = None
    for line in output.splitlines():
        if killed is None:
            match = re.search(r"\bKilled[:\s]+(\d+)", line)
            if match:
                killed = int(match.group(1))
        if survived is None:
            match = re.search(r"\bSurvived[:\s]+(\d+)", line)
            if match:
                survived = int(match.group(1))
        if kill_ratio is None:
            match = re.search(r"\bPercentage[:\s]*([\d.]+)\s*%", line)
            if match:
                kill_ratio = float(match.group(1))
    if killed is None and survived is None and kill_ratio is None:
        return None
    return {
        "killed": killed,
        "survived": survived,
        "kill_ratio": kill_ratio,
    }


_STATIC_SEVERITY_PATTERN = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<code>[A-Z][A-Z0-9]*)\s+(?P<message>.+)$"
)
_STATIC_EXPLICIT_PATTERN = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):\s+"
    r"(?P<severity>error|warning|note):\s+(?P<message>.+)$"
)
_STATIC_RUFF_NOCOL_PATTERN = re.compile(
    r"^(?P<path>[^:]+):(?P<line>\d+):\s+"
    r"(?P<code>[A-Z][A-Z0-9]*)\s+(?P<message>.+)$"
)
_STATIC_BARE_PATTERN = re.compile(r"^(?P<path>[^:]+):\s+(?P<message>.+)$")


def _parse_static_findings(text: str) -> list[dict[str, Any]]:
    """Best-effort parse of common analyzer output into structured findings.

    Recognises ruff/mypy-style ``path:line:col:``/``path:line:`` lines and a
    bare ``path: message`` fallback. Severity comes from the analyzer's own
    label (mypy ``error:``/``warning:``/``note:``) or the ruff code letter
    (``W`` → warning, ``I`` → info, else error). Unparseable lines are
    ignored — a parse miss is advisory, never fatal.
    """
    findings: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _STATIC_SEVERITY_PATTERN.match(stripped)
        if match is not None:
            code = match.group("code")
            severity = (
                "warning" if code.startswith("W")
                else "info" if code.startswith("I")
                else "error"
            )
            findings.append(
                {
                    "severity": severity,
                    "message": match.group("message"),
                    "path": match.group("path"),
                    "line": int(match.group("line")),
                }
            )
            continue
        match = _STATIC_EXPLICIT_PATTERN.match(stripped)
        if match is not None:
            findings.append(
                {
                    "severity": match.group("severity"),
                    "message": match.group("message"),
                    "path": match.group("path"),
                    "line": int(match.group("line")),
                }
            )
            continue
        match = _STATIC_RUFF_NOCOL_PATTERN.match(stripped)
        if match is not None:
            code = match.group("code")
            severity = (
                "warning" if code.startswith("W")
                else "info" if code.startswith("I")
                else "error"
            )
            findings.append(
                {
                    "severity": severity,
                    "message": match.group("message"),
                    "path": match.group("path"),
                    "line": int(match.group("line")),
                }
            )
            continue
        match = _STATIC_BARE_PATTERN.match(stripped)
        if match is not None:
            findings.append(
                {
                    "severity": "error",
                    "message": match.group("message"),
                    "path": match.group("path"),
                    "line": None,
                }
            )
    return findings


def _schema_check_changed_paths(
    worktree: JobWorktree,
    changed_paths: tuple[str, ...],
    checks: tuple[StaticSchemaCheck, ...],
) -> list[dict[str, Any]]:
    """Validate changed candidate files against declared schema checks.

    Stdlib only (M-ROADMAP): ``json`` / ``tomllib`` / ``xml.etree`` parse each
    changed file matching a check's glob; a parse failure becomes an ``error``
    finding. ``yaml`` uses TOML as its stdlib-safe surrogate (a full YAML /
    JSON-Schema engine runs via a declared analyzer command instead).
    """
    findings: list[dict[str, Any]] = []
    if not checks:
        return findings
    for check in checks:
        for relative_path in changed_paths:
            if not fnmatch.fnmatch(relative_path, check.path_glob):
                continue
            target = worktree.path / relative_path
            try:
                if check.schema_kind == "json":
                    with target.open("r", encoding="utf-8") as handle:
                        json.load(handle)
                elif check.schema_kind == "xml":
                    ElementTree.parse(str(target))
                else:  # "yaml" → TOML surrogate via stdlib tomllib
                    with target.open("rb") as handle:
                        tomllib.load(handle)
            except Exception as exc:  # noqa: BLE001 — report any parse failure
                findings.append(
                    {
                        "severity": "error",
                        "message": (
                            f"{check.schema_kind} schema validation failed: {exc}"
                        ),
                        "path": relative_path,
                        "line": None,
                    }
                )
    return findings


def _stream_text(value: Any) -> str:
    """Extract text from a command-result stream (dict or raw string)."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
    return ""


async def _run_command(
    settings: Settings,
    worktree: JobWorktree,
    command: VerificationCommand,
    *,
    project: ProjectPolicy | None = None,
    credential_names: tuple[str, ...] = (),
    property_seed: str | None = None,
) -> dict[str, Any]:
    """Run one verification command under the hermetic lab, opt-in per project.

    M-CC-09 additions (all opt-in on ``project``):
    - ``toolchain_pin`` replaces ``{python}`` with the pinned interpreter.
    - ``deps_cache_dir`` is exposed as ``DEPS_CACHE_DIR`` and, for sandboxed
      commands, bound read-only so it can be read but never written; a
      non-sandboxed command cannot guarantee read-only, so it fails closed.
    - ``network_policy`` "none" runs the command with no network namespace and
      fails closed when the command is not sandboxed; "allowlist" pins DNS to a
      generated allowlist hosts file + empty resolv.conf and likewise requires
      the sandbox.
    The fail-closed cases return a failed command result (no exit code), which
    fails the whole verification without granting any commit authority.
    M-ROADMAP: a ``property`` test-kind command receives a deterministic seed
    (``PROPERTY_TEST_SEED``, plus ``HYPOTHESIS_SEED`` when a Hypothesis-style
    runner is used) so its runs are reproducible.
    """
    python_path = _python_paths(worktree.path, command.python_path)
    extra_env: dict[str, str] | None = (
        {"PYTHONPATH": python_path} if python_path is not None else None
    )
    if property_seed is not None:
        merged = dict(extra_env) if extra_env is not None else {}
        merged["PROPERTY_TEST_SEED"] = property_seed
        merged["HYPOTHESIS_SEED"] = property_seed
        extra_env = merged
    argv = list(command.argv)

    hosts_file: str | None = None
    resolv_conf: str | None = None
    try:
        if project is not None:
            if project.toolchain_pin is not None and argv[0] == "{python}":
                argv[0] = _toolchain_python(project.toolchain_pin)
            if project.deps_cache_dir is not None:
                if not command.sandbox:
                    return _network_failed(
                        command.id,
                        "deps_cache_dir requires a sandboxed verification command",
                    )
                merged = dict(extra_env) if extra_env is not None else {}
                merged["DEPS_CACHE_DIR"] = project.deps_cache_dir
                extra_env = merged
            if project.network_policy == "none":
                if not command.sandbox:
                    return _network_failed(
                        command.id,
                        "network_policy=none requires a sandboxed verification "
                        "command (no network namespace available)",
                    )
            elif project.network_policy == "allowlist":
                if not command.sandbox:
                    return _network_failed(
                        command.id,
                        "network_policy=allowlist requires a sandboxed "
                        "verification command (DNS allowlist not enforceable)",
                    )
                hosts_file = _write_network_hosts_file(project.network_allowlist)
                resolv_conf = _write_empty_resolv_conf()
        if command.sandbox:
            argv = _sandbox_argv(
                worktree,
                argv,
                hosts_file=hosts_file,
                resolv_conf=resolv_conf,
                deps_cache_dir=(
                    project.deps_cache_dir if project is not None else None
                ),
            )
        return await _run_exec(
            settings,
            worktree,
            command_id=command.id,
            argv=argv,
            extra_env=extra_env,
            credential_names=credential_names,
            start_error_prefix="verification executable could not start",
        )
    finally:
        if hosts_file is not None:
            try:
                os.unlink(hosts_file)
            except OSError:
                pass
        if resolv_conf is not None:
            try:
                os.unlink(resolv_conf)
            except OSError:
                pass


async def _run_supply_chain_scan(
    settings: Settings,
    worktree: JobWorktree,
    scan: SupplyChainCommand,
    credential_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run one severity-gated supply-chain scan with a fixed argument vector.

    M-CC-11: a deterministic scanner (e.g. ``grype ... --fail-on=high``,
    ``pip-audit ...``) is invoked with fixed argv (no shell) inside the
    candidate worktree. A non-zero exit code means the gate rejects the commit
    (exactly how grype's ``--fail-on`` surfaces a HIGH/CRITICAL finding). The
    command must be present on PATH or start with ``{python}``; a scanner that
    cannot start is treated as a failed gate (fail-closed), not skipped.
    ``credential_names`` are the only credential env names projected from the
    bridge's environment into the scanner (M-CC-11 credential_policy).
    """
    return await _run_exec(
        settings,
        worktree,
        command_id=scan.id,
        argv=list(scan.argv),
        credential_names=credential_names,
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
        """Load persisted verification records, skipping unparseable rows.

        M-EV-02: also loads the ``verification_environment`` sidecar so each
        re-hydrated record carries its ``environment_digest``.

        M-EV-03: also loads the ``verification_bindings`` sidecar so each
        re-hydrated record carries its ``expires_at``/``authority_ref``
        evidence-binding envelope.
        """
        env_digests = {
            row["verification_id"]: row["environment_digest"]
            for row in store.load_verification_environment_rows(self._db)
        }
        bindings = {
            row["verification_id"]: row
            for row in store.load_verification_binding_rows(self._db)
        }
        for row in store.load_record_rows(self._db):
            verification_id = row["verification_id"]
            binding = bindings.get(verification_id)
            record = record_from_row(
                row,
                environment_digest=env_digests.get(verification_id),
                expires_at=binding.get("expires_at") if binding else None,
                authority_ref=binding.get("authority_ref") if binding else None,
            )
            if record is not None:
                self._records[record.verification_id] = record

    def _persist_record(self, record: VerificationRecord) -> None:
        """Persist one verification record as a single ACID transaction.

        Call this inside ``async with self._records_lock`` after mutating a
        record so the durable store mirrors the in-memory state. M-EV-02: also
        upserts the ``verification_environment`` sidecar row when the record
        carries an ``environment_digest``.

        M-EV-03: also upserts the ``verification_bindings`` sidecar row so the
        record's evidence-binding envelope (``toolchain_digest`` reusing the
        environment digest, ``reviewer_independence_class`` always NULL for a
        verification binding, ``expires_at``, ``authority_ref``) is durable. The
        binding is always written (even when the fields are NULL) so a reload
        sees the binding envelope exists; the commit-gate expiry check reads
        it back.
        """
        store.upsert_record_row(self._db, record_to_row(record))
        if record.environment_digest is not None:
            store.upsert_verification_environment_row(
                self._db,
                {
                    "verification_id": record.verification_id,
                    "environment_digest": record.environment_digest,
                },
            )
        store.upsert_verification_binding_row(
            self._db,
            {
                "verification_id": record.verification_id,
                # Reuse the M-EV-02 environment/toolchain digest concept.
                "toolchain_digest": record.environment_digest,
                # Verification bindings never carry an independence class —
                # only reviews do. Kept in the shared schema for uniformity.
                "reviewer_independence_class": None,
                "expires_at": record.expires_at,
                "authority_ref": record.authority_ref,
            },
        )
        if record.static_analysis:
            store.upsert_verification_static_analysis_row(
                self._db,
                {
                    "verification_id": record.verification_id,
                    "results_json": json.dumps(
                        list(record.static_analysis), ensure_ascii=False
                    ),
                },
            )

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

    async def _candidate_state_or_rollback(
        self,
        project: ProjectPolicy,
        job_id: str,
        worktree: JobWorktree,
    ) -> CandidateState:
        """M-CC-10: read the candidate state, rolling a corrupt candidate back.

        Wraps ``_candidate_state`` so a candidate that cannot be re-derived
        because its worktree state is corrupt (staged index, changed-while-read,
        unresolved index entry, symlink, vanished path) is rolled back to the
        last verified checkpoint — the worktree is reset to the clean base the
        checkpoint ledger describes and the job is paused resumably — instead of
        being left dangling. The original ``VerificationError`` is re-raised so
        the orchestrator still sees the failure; the rollback makes it
        recoverable. A rollback failure is swallowed so it never masks the
        original corruption error.
        """
        try:
            return await _candidate_state(self.settings, project, worktree)
        except VerificationError as exc:
            if _is_candidate_corruption(exc):
                try:
                    await self._jobs.rollback_candidate(project, job_id, str(exc))
                except Exception:
                    pass
            raise

    async def _run_static_analysis(
        self,
        project: ProjectPolicy,
        worktree: JobWorktree,
        changed_paths: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Run declared static-analysis commands + schema checks over the candidate.

        M-ROADMAP (internal; no new tool). Advisory: findings never fail the
        verification unless the project opted into ``static_analysis.fail_on =
        "error"``. Declared analyzers run read-only over the candidate via the
        SAME hermetic runner as the verification commands (PIN_TOOLCHAIN /
        DEPS_CACHE_DIR / NETWORK_POLICY / bwrap / deterministic env); their
        output is parsed best-effort into a structured findings list and the
        counts are rolled up. Schema checks validate each changed candidate
        file matching a glob with stdlib only. ``project.static_analysis`` is
        ``None`` (the default) → returns empty results, no-op.
        """
        analysis: StaticAnalysis | None = project.static_analysis
        if analysis is None:
            return [], {"error_count": 0, "warning_count": 0}
        results: list[dict[str, Any]] = []
        error_count = 0
        warning_count = 0
        redacted_paths = (
            worktree.path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )
        for command in analysis.commands:
            result = await _run_command(
                self.settings,
                worktree,
                command,
                project=project,
                credential_names=_credential_names(project),
            )
            bounded = _bound_redacted_output(
                _redact(result, redacted_paths),
                self.settings.max_verification_output_bytes,
            )
            entry: dict[str, Any] = {
                "command_id": command.id,
                "kind": command.kind.value,
                "status": bounded.get("status"),
                "exit_code": bounded.get("exit_code"),
                "findings": _parse_static_findings(
                    _stream_text(bounded.get("stdout"))
                    + "\n"
                    + _stream_text(bounded.get("stderr"))
                ),
                "error": bounded.get("error"),
            }
            error_count += sum(
                1 for f in entry["findings"] if f["severity"] == "error"
            )
            warning_count += sum(
                1 for f in entry["findings"] if f["severity"] == "warning"
            )
            results.append(entry)
        schema_findings = _schema_check_changed_paths(
            worktree, changed_paths, analysis.schema_checks
        )
        error_count += sum(
            1 for f in schema_findings if f["severity"] == "error"
        )
        warning_count += sum(
            1 for f in schema_findings if f["severity"] == "warning"
        )
        if schema_findings:
            schema_errors = any(
                f["severity"] == "error" for f in schema_findings
            )
            results.append(
                {
                    "command_id": None,
                    "kind": "schema",
                    "status": "failed" if schema_errors else "passed",
                    "exit_code": 1 if schema_errors else 0,
                    "findings": schema_findings,
                    "error": None,
                }
            )
        return results, {"error_count": error_count, "warning_count": warning_count}

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
                # MCPB-012: origin-aware clean-source exception. A retained
                # candidate whose trusted durable origin is the working-tree
                # capture is verifiable while the canonical checkout remains
                # dirty (the capture left it that way by design); ordinary
                # candidates keep the clean-source rule.
                allow_dirty_source = await self._jobs.is_working_tree_capture_candidate(
                    project, job_id
                )
                await self._worktrees.validate_resume(
                    project, worktree, allow_dirty_source=allow_dirty_source
                )
                before = await self._candidate_state_or_rollback(
                    project, job_id, worktree
                )
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
                    for repetition in range(repetitions):
                        # M-ROADMAP: a property command runs with a
                        # deterministic seed derived from the base commit + its
                        # own id (repetition-suffixed so each repetition widens
                        # coverage without losing reproducibility).
                        property_seed: str | None = None
                        if command.kind is TestKind.PROPERTY:
                            seed_source = f"{before.base_head}:{command.id}"
                            if repetitions > 1:
                                seed_source = f"{seed_source}:{repetition}"
                            property_seed = hashlib.sha256(
                                seed_source.encode("utf-8")
                            ).hexdigest()
                        result = await _run_command(
                            self.settings,
                            worktree,
                            command,
                            project=project,
                            credential_names=_credential_names(project),
                            property_seed=property_seed,
                        )
                        bounded = _bound_redacted_output(
                            _redact(result, redacted_paths),
                            self.settings.max_verification_output_bytes,
                        )
                        if first is None:
                            first = bounded
                        elif self._commands_diverge(first, bounded):
                            nondeterminism_labeled = True
                    # M-EV-02: per-command structured fields (additive aliases +
                    # digest). ``id`` aliases the existing ``command_id``;
                    # ``command_digest`` is the SHA-256 over the declared argv
                    # so a changed command vector is detectable. M-ROADMAP: the
                    # test-kind label rides on every command result, and a
                    # mutation command's parsed stats (advisory) when available.
                    if first is not None:
                        first["id"] = first.get("command_id", command.id)
                        first["command_digest"] = _command_digest(command)
                        first["kind"] = command.kind.value
                        if command.kind is TestKind.MUTATION:
                            first["mutation_stats"] = _parse_mutation_stats(first)
                    command_results.append(first)
                results = tuple(command_results)
                after = await self._candidate_state_or_rollback(
                    project, job_id, worktree
                )
                # M-ROADMAP: advisory static analysis runs after the
                # verification commands AND after the after-state snapshot, so
                # analyzers' read-only runs never affect the state-unchanged
                # check. ``fail_on="error"`` (opt-in) flips the conclusion to
                # FAIL; the default keeps findings advisory.
                static_analysis_results: list[dict[str, Any]] = []
                static_analysis_summary: dict[str, int] = {
                    "error_count": 0,
                    "warning_count": 0,
                }
                static_analysis_failed = False
                if project.static_analysis is not None:
                    (
                        static_analysis_results,
                        static_analysis_summary,
                    ) = await self._run_static_analysis(
                        project, worktree, before.paths
                    )
                    static_analysis_failed = (
                        project.static_analysis.fail_on == "error"
                        and static_analysis_summary["error_count"] > 0
                    )
            except (DevelopmentJobError, WorktreeError, GitCommandError) as exc:
                raise VerificationError(str(exc)) from exc

            unchanged = before == after
            passed = (
                unchanged
                and all(result["status"] == "passed" for result in results)
                and not static_analysis_failed
            )
            # M-EV-02: environment/toolchain fingerprint (SHA-256 over the
            # Python interpreter version + the allowlisted deterministic env
            # keys + the verification command argv). Stable across identical-env
            # runs; changes when the declared argv changes.
            # M-CC-09: when the project pins a toolchain, the pin's digest is
            # folded in so a pinned-toolchain run carries a stable toolchain
            # fingerprint that changes only when the pin changes.
            toolchain_pin_digest = (
                _toolchain_pin_digest(project.toolchain_pin)
                if project.toolchain_pin is not None
                else None
            )
            environment_digest = compute_environment_digest(
                project.verification_commands,
                toolchain_pin_digest=toolchain_pin_digest,
            )
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
                # M-CC-09 hermetic-lab response fields (additive, path-free):
                # the toolchain-pin digest (a hash, never the pin path) and the
                # network policy label.
                "toolchain_pin_digest": toolchain_pin_digest,
                "network_policy": project.network_policy,
                # M-EV-02: structured verification result schema (additive
                # aliases/fields; existing fields are preserved).
                "candidate_fingerprint": before.fingerprint,
                "base_commit": before.base_head,
                "environment_digest": environment_digest,
                "conclusion": "PASS" if passed else "FAIL",
                # M-ROADMAP: advisory static-analysis results (empty when the
                # project declares none) — surfaced for visibility, never part
                # of the commit-authority envelope.
                "static_analysis": static_analysis_results,
                "static_analysis_summary": static_analysis_summary,
            }
            if not passed:
                if not unchanged:
                    response["error"] = "verification commands changed the candidate state"
                elif static_analysis_failed:
                    response["error"] = (
                        "static analysis reported errors (fail_on=error)"
                    )
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
                environment_digest=environment_digest,
                static_analysis=tuple(static_analysis_results),
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

    async def replay(
        self,
        project: ProjectPolicy,
        verification_id: str,
    ) -> dict[str, Any]:
        """M-ROADMAP: re-run a recorded verification read-only (deterministic replay).

        A recorded verification persists its ``environment_digest``, per-command
        ``command_digest``, and outcomes — but nothing ever re-runs it. This
        method re-runs the recorded commands in the hermetic lab (the SAME
        ``_run_command``/``_run_exec`` path ``verify()`` uses, so replay inherits
        PIN_TOOLCHAIN / DEPS_CACHE_DIR / NETWORK_POLICY / bwrap / deterministic
        env) against a fresh read-only detached checkout at the recorded
        ``base_head``, then reports whether the persisted evidence envelope
        still reproduces.

        Replay does NOT re-derive the candidate worktree or re-verify content —
        that is ``verify()``. A command whose outcome depends on the candidate's
        un-persisted edits is reported honestly as not reproducing from the
        persisted envelope alone. Command argv edits since the verification are
        surfaced as digest mismatches and are never silently re-run. No
        canonical, candidate, or publication state is written.
        """
        async with self._records_lock:
            record = self._records.get(verification_id)
            if record is None:
                raise VerificationError("unknown verification_id")
            if record.project_id != project.id:
                raise VerificationError(
                    "verification_id does not belong to this project"
                )

        # Recompute the environment digest over the project's CURRENT declared
        # commands + toolchain pin. A changed command set or pin therefore
        # surfaces as an environment digest mismatch — never a silent re-run.
        toolchain_pin_digest = (
            _toolchain_pin_digest(project.toolchain_pin)
            if project.toolchain_pin is not None
            else None
        )
        recomputed_env_digest = compute_environment_digest(
            project.verification_commands,
            toolchain_pin_digest=toolchain_pin_digest,
        )
        environment_match = (
            record.environment_digest is not None
            and record.environment_digest == recomputed_env_digest
        )

        declared = {command.id: command for command in project.verification_commands}
        # A deterministic per-record path; a stale replay worktree (from a hard
        # crash) is removed first so replay is repeat-safe.
        replay_job_id = f"replay-{record.verification_id[:12]}"
        replay_path = (
            self._worktrees._root_for(project) / project.id / replay_job_id
        )
        try:
            await self._worktrees.remove(
                project,
                JobWorktree(
                    path=replay_path,
                    base_head=record.base_head,
                    read_only=True,
                ),
            )
            replay_worktree = await self._worktrees.recreate_detached(
                project, replay_job_id, record.base_head
            )
        except WorktreeError as exc:
            raise VerificationError(str(exc)) from exc
        replay_worktree = JobWorktree(
            path=replay_worktree.path,
            base_head=replay_worktree.base_head,
            read_only=True,
        )
        redacted_paths = (
            replay_worktree.path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

        notes: list[str] = []
        command_entries: list[dict[str, Any]] = []
        try:
            for recorded_command in record.commands:
                command_id = recorded_command.get("id") or recorded_command.get(
                    "command_id"
                )
                entry: dict[str, Any] = {
                    "command_id": command_id,
                    "reconstructed": False,
                    "digest_match": False,
                    "result_match": False,
                    "replayed_status": None,
                    "replayed_exit_code": None,
                    "notes": [],
                }
                command = declared.get(command_id) if command_id else None
                if command is None:
                    entry["notes"].append(
                        "no declared verification command with this id exists today; "
                        "not re-run"
                    )
                    command_entries.append(entry)
                    continue
                entry["reconstructed"] = True
                entry["kind"] = command.kind.value
                digest_match = (
                    _command_digest(command)
                    == recorded_command.get("command_digest")
                )
                entry["digest_match"] = digest_match
                if not digest_match:
                    entry["notes"].append(
                        "command argv/id changed since the verification was "
                        "recorded; not re-run"
                    )
                    command_entries.append(entry)
                    continue
                # M-ROADMAP: replay a property command under the same
                # deterministic seed verify() used (the recorded result is the
                # first repetition, whose seed carries no repetition suffix), so
                # its replay is reproducible.
                property_seed: str | None = None
                if command.kind is TestKind.PROPERTY:
                    property_seed = hashlib.sha256(
                        f"{record.base_head}:{command.id}".encode("utf-8")
                    ).hexdigest()
                result = await _run_command(
                    self.settings,
                    replay_worktree,
                    command,
                    project=project,
                    credential_names=_credential_names(project),
                    property_seed=property_seed,
                )
                bounded = _bound_redacted_output(
                    _redact(result, redacted_paths),
                    self.settings.max_verification_output_bytes,
                )
                entry["replayed_status"] = bounded.get("status")
                entry["replayed_exit_code"] = bounded.get("exit_code")
                entry["result_match"] = (
                    bounded.get("status") == recorded_command.get("status")
                    and bounded.get("exit_code") == recorded_command.get("exit_code")
                )
                if not entry["result_match"]:
                    entry["notes"].append(
                        "replayed outcome differs from the recorded outcome "
                        "(replay runs against the recorded base, not the candidate)"
                    )
                command_entries.append(entry)
        finally:
            try:
                await self._worktrees.remove(project, replay_worktree)
            except WorktreeError:
                # Cleanup is best-effort: a leftover read-only replay checkout is
                # never trusted, only removed on a later replay of the same id.
                pass

        reproducible = environment_match and all(
            entry["reconstructed"]
            and entry["digest_match"]
            and entry["result_match"]
            for entry in command_entries
        )
        if not environment_match:
            notes.append(
                "environment digest differs: the recorded verification ran in a "
                "different environment (interpreter, deterministic env keys, "
                "command definitions, or toolchain pin)"
            )
        return {
            "verification_id": record.verification_id,
            "project_id": record.project_id,
            "job_id": record.job_id,
            "base_head": record.base_head,
            "environment_digest": {
                "recorded": record.environment_digest,
                "recomputed": recomputed_env_digest,
                "match": environment_match,
            },
            "commands": command_entries,
            "reproducible": reproducible,
            "notes": notes,
        }

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
            # M-EV-03: reject a verification binding whose expiry has passed.
            # A NULL/absent ``expires_at`` means no expiry → allowed.
            if _is_expired(record.expires_at):
                raise VerificationError(
                    "verification evidence binding has expired"
                )
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
                # MCPB-012: origin-aware clean-source exception (mirrors
                # ``verify``) so a captured candidate can be committed while
                # the canonical checkout remains dirty with the captured
                # source state; ordinary candidates keep the clean-source rule.
                allow_dirty_source = (
                    await self._jobs.is_working_tree_capture_candidate(
                        project, job_id
                    )
                )
                await self._worktrees.validate_resume(
                    project, worktree, allow_dirty_source=allow_dirty_source
                )
                current = await _candidate_state(self.settings, project, worktree)
            except (DevelopmentJobError, WorktreeError, GitCommandError) as exc:
                raise VerificationError(str(exc)) from exc

            # M-CC-06: fail-closed unexpected-files gate. A changed path matching
            # the project's declared ``unexpected_paths`` policy (generated
            # artifacts, ``*.lock``, ``dist/``, ``node_modules/``, ...) that is
            # NOT part of the verified, declared changed set is rejected here,
            # before the fingerprint comparison, so the failure is explicit and
            # diagnostic. The declared set is the verification record's ordered
            # path list — the exact set the verifier examined and any reviewer
            # signed off on. A declared unexpected path (one that was verified)
            # is allowed. Empty ``unexpected_paths`` (the default) never rejects.
            # The existing submodule rejection in the candidate-state derivation
            # stays.
            undeclared_unexpected = _undeclared_unexpected_paths(
                project, current.paths, record.paths
            )
            if undeclared_unexpected:
                await self._jobs.quarantine_provenance(
                    project,
                    job_id,
                    "undeclared unexpected file(s) matching the project's "
                    "unexpected_paths policy",
                )
                raise VerificationError(
                    "commit gate rejected unexpected file(s) matching the "
                    "project's unexpected_paths policy: "
                    + ", ".join(undeclared_unexpected)
                )

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
                        self.settings,
                        worktree,
                        scan,
                        credential_names=_credential_names(project),
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
