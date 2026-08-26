"""Governed host runtime verification for the local development bridge (MCPB-011).

A runtime verification runs **one named, policy-authorized check** against a
project's **canonical checkout** and returns content-addressed PASS/FAIL
evidence. It exists for checks that cannot run inside an isolated Claude
development worktree because they need host-runtime access — the Docker daemon,
loopback networking, host-managed services, integration-test infrastructure.

This is deliberately not a shell. The MCP caller selects only ``project_id``,
``check_id``, and ``expected_head``; the executable, argv, environment, working
directory, and timeout all come from trusted local policy
(``RuntimeVerificationCommand``). The command is executed directly with
``asyncio.create_subprocess_exec`` (no ``shell=True``, no ``bash -c``, no
``os.system``).

Canonical binding + repository immutability mirror the proven model in
``SyncManager.sync``: the canonical checkout must be clean, at exactly the
caller-supplied ``expected_head``, and (where BridgeLab knows the default
branch) on that branch. Only read-only git is used before execution — never
fetch/pull/merge/checkout/switch/stash/clean. After execution, HEAD, the
working tree, and the branch are re-checked; any drift invalidates the evidence
(no auto-restore, no PASS).

Governance separation: a runtime verification ID lives in the ``rtv_`` namespace
and cannot match the candidate ``verification_id`` pattern, so it can never be
fed to ``commit_verified_development_job``. A successful runtime check produces
evidence only — it never stages, commits, updates refs, branches, pushes,
merges, tags, opens PRs, or mutates policy.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store
from .config import Settings
from .git import GitCommandError, run_git
from .models import ProjectPolicy, RuntimeVerificationCommand
from .registry import ProjectLockManager
from .verification import _redact, _remote_default_branch
from .worktrees import GitWorktreeManager


class RuntimeVerificationError(RuntimeError):
    """Raised when a runtime verification cannot be safely executed or evidenced."""


# Runtime verification IDs are prefixed so they can never be confused with the
# candidate ``verification_id`` namespace (``^[0-9a-f]{32}$``). A
# ``commit_verified_development_job`` caller cannot pass one as a verification_id
# because the pattern rejects the ``rtv_`` prefix at the schema boundary.
_RUNTIME_VERIFICATION_ID_PREFIX = "rtv_"

# Policy environment values shorter than this are not redacted from captured
# output (they are configuration flags like "1" or "host"; redacting them would
# mangle legitimate test output). Longer values are likely tokens, credentials,
# or absolute host paths, so they are redacted from stdout/stderr.
_SECRET_REDACT_MIN_LEN = 12


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


async def _read_stream(stream: Any, limit: int) -> dict[str, Any]:
    """Read a process stream with bounded capture and a total byte count.

    Capture up to ``limit + 8192`` bytes (one max path beyond the public limit
    so redaction can still match a path straddling the boundary) and record the
    total bytes emitted. The SHA-256 is computed by the caller over the final
    (redacted, bounded) text so a secret can never be confirmed via the hash.
    """
    capture_limit = limit + 8192
    captured = bytearray()
    total = 0
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        remaining = capture_limit - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return {
        "text": bytes(captured).decode("utf-8", errors="replace"),
        "bytes": total,
        "truncated": total > limit,
    }


def _bound_output(value: dict[str, Any], limit: int) -> dict[str, Any]:
    """Bound a captured stream's text to ``limit`` bytes (UTF-8 safe)."""
    text = value.get("text")
    if isinstance(text, str):
        encoded = text.encode("utf-8")
        if len(encoded) > limit:
            value["text"] = encoded[:limit].decode("utf-8", errors="ignore")
            value["truncated"] = True
    return value


def _finalize_output(value: dict[str, Any], limit: int) -> dict[str, Any]:
    """Bound a (already redacted) stream dict and stamp its content hash.

    The hash is over the final redacted, bounded text — never over the raw
    output — so the hash cannot act as a confirmation oracle for a secret that
    was redacted, and a recipient can recompute it from the returned text.
    """
    _bound_output(value, limit)
    text = value.get("text")
    value["sha256"] = _sha256(text if isinstance(text, str) else "")
    return value


def _secret_values(command: RuntimeVerificationCommand) -> tuple[str, ...]:
    """Policy env values long enough to plausibly be a secret or host path."""
    return tuple(
        value
        for value in command.environment.values()
        if len(value) >= _SECRET_REDACT_MIN_LEN
    )


def _redact_secrets(value: Any, secrets: tuple[str, ...]) -> Any:
    if not secrets:
        return value
    if isinstance(value, str):
        for token in secrets:
            value = value.replace(token, "<redacted>")
        return value
    if isinstance(value, dict):
        return {key: _redact_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item, secrets) for item in value)
    return value


def _build_environment(policy_env: dict[str, str]) -> dict[str, str]:
    """Minimal deterministic host environment plus policy overrides.

    The full BridgeLab process environment is NOT inherited (no token, cert, or
    secret leakage). ``PATH`` and ``HOME`` come from the bridge env so host
    tools (e.g. ``docker``) are discoverable and per-user config resolves; the
    rest is a deterministic base. Policy-defined values are applied last and win
    over the base (but never over the forbidden interpreter/loader keys, which
    ``RuntimeVerificationCommand`` rejects at validation time).
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
    }
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    env.update(policy_env)
    return env


def _policy_fingerprint(project: ProjectPolicy) -> str:
    """Content-address the effective runtime-verification configuration.

    A SHA-256 over the sorted JSON model dump of the project's runtime
    verification commands. A hash, so it leaks no environment values; it lets a
    caller detect any change to the effective policy that authorized a check.
    """
    payload = [
        command.model_dump(mode="json")
        for command in sorted(
            project.runtime_verification_commands, key=lambda c: c.id
        )
    ]
    return _sha256(_json_dumps(payload))


async def _canonical_state(
    root: Path, *, timeout: int
) -> tuple[str, str, str]:
    """Return ``(branch, head, porcelain_status)`` for the canonical checkout.

    ``branch`` is the empty string when HEAD is detached. ``porcelain_status``
    is ``git status --porcelain=v1 --untracked-files=all`` output, so a clean
    checkout (including no untracked files) yields the empty string. Read-only.
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


class RuntimeVerificationManager:
    """Executes named runtime checks against a canonical checkout, evidence-only.

    Owns the additive ``runtime_verifications`` audit table in ``bridge.db``
    and re-hydrates it synchronously in ``__init__`` (append-only, one row per
    run), exactly as the sync/publication managers re-hydrate their own state.
    """

    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._project_roots = tuple(project.root for project in projects)
        self._records: list[dict[str, Any]] = []
        self._records_lock = asyncio.Lock()
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_records()

    def _reload_records(self) -> None:
        for row in store.load_runtime_verification_rows(self._db):
            self._records.append(row)

    def _redaction_paths(self, project: ProjectPolicy) -> tuple[Path, ...]:
        return (
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    def _resolve_check(
        self, project: ProjectPolicy, check_id: str
    ) -> RuntimeVerificationCommand:
        for command in project.runtime_verification_commands:
            if command.id == check_id:
                return command
        raise RuntimeVerificationError(
            f"unknown runtime verification check_id {check_id!r} for project "
            f"{project.id!r}"
        )

    async def _execute(
        self, project: ProjectPolicy, command: RuntimeVerificationCommand
    ) -> dict[str, Any]:
        """Run the fixed-argv command directly (no shell) and bound its output."""
        argv = list(command.argv)
        if argv[0] == "{python}":
            argv[0] = sys.executable
        environment = _build_environment(command.environment)
        timeout_seconds = (
            command.timeout_seconds
            or self.settings.runtime_verification_command_timeout_seconds
        )
        out_limit = self.settings.max_runtime_verification_output_bytes

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(project.root),
                env=environment,
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return {
                "launched": False,
                "exit_code": None,
                "stdout": None,
                "stderr": None,
                "timed_out": False,
                "error": f"executable could not start: {exc}",
            }

        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(_read_stream(process.stdout, out_limit))
        stderr_task = asyncio.create_task(_read_stream(process.stderr, out_limit))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
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
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await process.wait()
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return {
            "launched": True,
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "error": None,
        }

    @staticmethod
    def _failure_error(
        *,
        executed: bool,
        exec_result: dict[str, Any] | None,
        repo_unchanged: bool,
        repo_read_error: str | None,
    ) -> str:
        if not executed:
            # Binding failures carry their own error text set by the caller.
            return exec_result["error"] if exec_result else "runtime verification rejected"
        if exec_result is not None and not exec_result["launched"]:
            return exec_result["error"]
        if exec_result is not None and exec_result["timed_out"]:
            return "runtime verification command timed out"
        if repo_read_error is not None:
            return repo_read_error
        if not repo_unchanged:
            return "canonical repository state changed during runtime verification"
        if exec_result is not None and exec_result["exit_code"] != 0:
            return f"runtime verification command exited with code {exec_result['exit_code']}"
        return "runtime verification failed"

    async def run(
        self,
        project: ProjectPolicy,
        check_id: str,
        expected_head: str,
    ) -> dict[str, Any]:
        """Resolve, bind to canonical HEAD, execute one named check, evidence it."""
        command = self._resolve_check(project, check_id)
        expected = expected_head.lower()
        git_timeout = self.settings.git_command_timeout_seconds
        redaction_paths = self._redaction_paths(project)
        policy_fingerprint = _policy_fingerprint(project)
        out_limit = self.settings.max_runtime_verification_output_bytes

        lock = await self._locks.for_project(project.id)
        async with lock:
            started_at = _timestamp()

            # --- Canonical binding (read-only git only). ---
            pre_state = await self._read_canonical(project.root, git_timeout)
            if pre_state is None:
                return await self._record(
                    project, check_id, command, expected, pre_state, None,
                    executed=False, exec_result=None, status="failed",
                    error="canonical repository state could not be read",
                    repo_unchanged=False, redaction_paths=redaction_paths,
                    policy_fingerprint=policy_fingerprint,
                    started_at=started_at, out_limit=out_limit,
                )
            # _canonical_state returns (branch, head, status); unpack in that
            # order so the HEAD/branch binding checks compare the right fields.
            pre_branch, pre_head, pre_status = pre_state
            binding_error = await self._binding_error(
                project, expected, pre_head, pre_status, pre_branch, git_timeout
            )
            if binding_error is not None:
                return await self._record(
                    project, check_id, command, expected, pre_state, None,
                    executed=False, exec_result=None, status="failed",
                    error=binding_error, repo_unchanged=False,
                    redaction_paths=redaction_paths,
                    policy_fingerprint=policy_fingerprint,
                    started_at=started_at, out_limit=out_limit,
                )

            # --- Execute the fixed configured command (no shell). ---
            exec_result = await self._execute(project, command)

            # --- Revalidate repository immutability. ---
            post_state = await self._read_canonical(project.root, git_timeout)
            if post_state is None:
                repo_unchanged = False
                repo_read_error = (
                    "canonical repository state could not be re-read after "
                    "execution; cannot confirm immutability"
                )
            else:
                post_branch, post_head, post_status = post_state
                repo_unchanged = (
                    post_head == pre_head
                    and post_status == ""
                    and post_branch == pre_branch
                )
                repo_read_error = None

            completed_at = _timestamp()
            launched = exec_result["launched"]
            timed_out = exec_result["timed_out"]
            exit_code = exec_result["exit_code"]
            passed = (
                launched
                and not timed_out
                and exit_code == 0
                and repo_unchanged
                and repo_read_error is None
            )
            error = None if passed else self._failure_error(
                executed=True,
                exec_result=exec_result,
                repo_unchanged=repo_unchanged,
                repo_read_error=repo_read_error,
            )
            return await self._record(
                project, check_id, command, expected, pre_state, post_state,
                executed=True, exec_result=exec_result,
                status="passed" if passed else "failed",
                error=error, repo_unchanged=repo_unchanged,
                redaction_paths=redaction_paths,
                policy_fingerprint=policy_fingerprint,
                started_at=started_at, completed_at=completed_at,
                out_limit=out_limit,
            )

    async def _read_canonical(
        self, root: Path, timeout: int
    ) -> tuple[str, str, str] | None:
        try:
            return await _canonical_state(root, timeout=timeout)
        except GitCommandError:
            return None

    async def _binding_error(
        self,
        project: ProjectPolicy,
        expected: str,
        pre_head: str,
        pre_status: str,
        pre_branch: str,
        timeout: int,
    ) -> str | None:
        if pre_status:
            return (
                "canonical checkout must be clean before runtime verification; "
                "refusing to run against a dirty working tree"
            )
        if pre_head.lower() != expected:
            return (
                f"canonical HEAD {pre_head[:12]} does not match expected_head "
                f"{expected[:12]}"
            )
        # Where BridgeLab knows the default branch, require the canonical
        # checkout to be on it. Local-only repos (no origin/HEAD) fail open.
        default_branch = await _remote_default_branch(
            project.root, timeout_seconds=timeout
        )
        if default_branch is not None and pre_branch != default_branch:
            current = pre_branch or "detached HEAD"
            return (
                f"canonical checkout must be on the default branch "
                f"{default_branch!r} for runtime verification; currently on "
                f"{current!r}"
            )
        return None

    async def _record(
        self,
        project: ProjectPolicy,
        check_id: str,
        command: RuntimeVerificationCommand,
        expected: str,
        pre_state: tuple[str, str, str] | None,
        post_state: tuple[str, str, str] | None,
        *,
        executed: bool,
        exec_result: dict[str, Any] | None,
        status: str,
        error: str | None,
        repo_unchanged: bool,
        redaction_paths: tuple[Path, ...],
        policy_fingerprint: str,
        started_at: str,
        out_limit: int,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        """Build, redact, persist, and return one runtime-verification record."""
        rtv_id = _RUNTIME_VERIFICATION_ID_PREFIX + secrets.token_hex(16)
        # pre_state/post_state are (branch, head, status); head is index 1.
        verified_head = pre_state[1] if pre_state is not None else None
        pre_head = pre_state[1] if pre_state is not None else None
        pre_branch = pre_state[0] if pre_state is not None else None
        pre_status = pre_state[2] if pre_state is not None else None
        post_head = post_state[1] if post_state is not None else None
        post_branch = post_state[0] if post_state is not None else None
        post_status = post_state[2] if post_state is not None else None

        stdout_raw = exec_result["stdout"] if exec_result else None
        stderr_raw = exec_result["stderr"] if exec_result else None
        exit_code = exec_result["exit_code"] if exec_result else None

        repository = {
            "pre_head": pre_head,
            "pre_branch": pre_branch,
            "pre_clean": pre_status == "" if pre_status is not None else None,
            "post_head": post_head,
            "post_branch": post_branch,
            "post_clean": post_status == "" if post_status is not None else None,
            "unchanged": repo_unchanged,
            "mutated": executed and not repo_unchanged,
        }

        evidence: dict[str, Any] = {
            "runtime_verification_id": rtv_id,
            "project_id": project.id,
            "check_id": check_id,
            "expected_head": expected,
            "verified_head": verified_head,
            "status": status,
            "executed": executed,
            "exit_code": exit_code,
            "started_at": started_at,
            "completed_at": completed_at,
            "stdout": stdout_raw,
            "stderr": stderr_raw,
            "stdout_hash": None,
            "stderr_hash": None,
            "policy_fingerprint": policy_fingerprint,
            "evidence_fingerprint": None,
            "repository": repository,
            "timeout_seconds": (
                command.timeout_seconds
                or self.settings.runtime_verification_command_timeout_seconds
            ),
            "error": error,
        }

        # Redact paths (existing infrastructure) then long policy env values
        # (secrets / host paths) from captured output, bound, and stamp content
        # hashes over the final (non-sensitive) text.
        redacted = _redact(evidence, redaction_paths)
        redacted = _redact_secrets(redacted, _secret_values(command))
        stdout = redacted.get("stdout")
        stderr = redacted.get("stderr")
        if isinstance(stdout, dict):
            redacted["stdout"] = _finalize_output(stdout, out_limit)
            redacted["stdout_hash"] = redacted["stdout"]["sha256"]
        if isinstance(stderr, dict):
            redacted["stderr"] = _finalize_output(stderr, out_limit)
            redacted["stderr_hash"] = redacted["stderr"]["sha256"]

        # Deterministic evidence fingerprint, bound to project, check, HEAD,
        # effective policy, process result, and output hashes — never timestamps.
        fingerprint_payload = {
            "project_id": project.id,
            "check_id": check_id,
            "verified_head": verified_head,
            "policy_fingerprint": policy_fingerprint,
            "status": status,
            "exit_code": exit_code,
            "stdout_hash": redacted["stdout_hash"],
            "stderr_hash": redacted["stderr_hash"],
            "repository_unchanged": repo_unchanged,
        }
        redacted["evidence_fingerprint"] = _sha256(_json_dumps(fingerprint_payload))

        self._persist(
            rtv_id,
            project,
            check_id,
            expected,
            redacted,
        )
        return redacted

    def _persist(
        self,
        rtv_id: str,
        project: ProjectPolicy,
        check_id: str,
        expected: str,
        evidence: dict[str, Any],
    ) -> None:
        stdout = evidence.get("stdout")
        stderr = evidence.get("stderr")
        row = {
            "runtime_verification_id": rtv_id,
            "project_id": project.id,
            "check_id": check_id,
            "expected_head": expected,
            "verified_head": evidence.get("verified_head") or "",
            "status": evidence["status"],
            "exit_code": evidence.get("exit_code"),
            "started_at": evidence["started_at"],
            "completed_at": evidence.get("completed_at") or evidence["started_at"],
            "stdout_json": json.dumps(stdout, ensure_ascii=False),
            "stderr_json": json.dumps(stderr, ensure_ascii=False),
            "stdout_hash": evidence.get("stdout_hash") or "",
            "stderr_hash": evidence.get("stderr_hash") or "",
            "policy_fingerprint": evidence["policy_fingerprint"],
            "evidence_fingerprint": evidence["evidence_fingerprint"],
            "repository_json": json.dumps(evidence["repository"], ensure_ascii=False),
            "verified_at": evidence.get("completed_at") or evidence["started_at"],
        }
        store.insert_runtime_verification_row(self._db, row)
        # Bounded in-memory audit cache (keep it small: last 1000 rows per manager).
        self._records.append(row)
        if len(self._records) > 1000:
            del self._records[: len(self._records) - 1000]

    def audit(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Return persisted runtime-verification rows as dicts, oldest first.

        Read-only accessor for inspection; the ``runtime_verifications`` table
        is append-only. ``stdout_json``/``stderr_json`` are returned as-is
        (already bounded and redacted at write time).
        """
        return list(store.load_runtime_verification_rows(self._db, project_id))