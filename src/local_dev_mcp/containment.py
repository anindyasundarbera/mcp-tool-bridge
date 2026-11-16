"""Executor containment primitives and bwrap process runner (A2.2.2).

Zero FastMCP imports.
Provides Linux user-namespace containment via bubblewrap (bwrap),
process group orphan lifecycle protection, minimal environment sanitization,
and containment policy enforcement.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
from typing import Any

from .executor_contract import ExecutionRequest, NetworkMode


class ContainmentUnavailableError(RuntimeError):
    """Raised when bwrap containment is requested but unavailable without fallback."""


class ContainmentMode(str, Enum):
    """Execution containment isolation mode."""

    BWRAP = "bwrap"
    TRUSTED_DEV_UNCONTAINED = "trusted_dev_uncontained"


class ContainmentProfile(str, Enum):
    """Containment profile labels for execution results."""

    BWRAP = "bwrap"
    UNCONTAINED_TRUSTED_DEV = "UNCONTAINED_TRUSTED_DEV"
    WORKTREE = "worktree"
    CREDENTIAL_PROJECTION = "credential_projection"
    SANDBOXED = "sandboxed"


SAFE_MINIMAL_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "USER",
    "LOGNAME",
)


def build_minimal_executor_env(
    host_env: Mapping[str, str] | None = None,
    *,
    explicit_projection: Mapping[str, str] | None = None,
    candidate_root: Path | None = None,
) -> dict[str, str]:
    """Construct a sanitized, least-privilege executor process environment.

    - Never copies full os.environ when credential_policy is absent.
    - Projects ONLY SAFE_MINIMAL_ENV_KEYS from host_env (with safe defaults for
      PATH and LANG if missing) plus keys explicitly passed in explicit_projection.
    - Guarantees undeclared host secrets (OPENAI_API_KEY, ANTHROPIC_API_KEY,
      GITHUB_TOKEN, NGROK_AUTHTOKEN, AWS_SECRET_ACCESS_KEY, SECRET_CANARY_*, etc.)
      are stripped.
    """
    source_env = os.environ if host_env is None else host_env
    env: dict[str, str] = {}

    for k in SAFE_MINIMAL_ENV_KEYS:
        if k in source_env:
            env[k] = source_env[k]

    if "PATH" not in env:
        env["PATH"] = os.defpath or "/usr/local/bin:/usr/bin:/bin"
    if "LANG" not in env:
        env["LANG"] = "C.UTF-8"

    if explicit_projection is not None:
        for k, v in explicit_projection.items():
            env[k] = str(v)

    return env


def containment_profile_for(request: ExecutionRequest) -> str:
    """Return the applicable containment profile description for an execution request."""
    if request.resource_limits.network_mode != NetworkMode.NONE:
        return ContainmentProfile.SANDBOXED.value
    if request.env_projection:
        return ContainmentProfile.CREDENTIAL_PROJECTION.value
    return ContainmentProfile.WORKTREE.value


def validate_candidate_isolation(request: ExecutionRequest) -> None:
    """Validate that the candidate root is isolated from the canonical root.

    Fail-closed adapter-side recheck: ensures roots are absolute and distinct.
    """
    if not request.candidate_root.is_absolute():
        raise ValueError("candidate_root must be an absolute path")
    if request.canonical_root is not None:
        if not request.canonical_root.is_absolute():
            raise ValueError("canonical_root must be an absolute path")
        if request.candidate_root.resolve() == request.canonical_root.resolve():
            raise ValueError("candidate_root and canonical_root must differ")


def is_path_within_candidate(candidate_root: Path, relative: str) -> bool:
    """Return True when a relative path resolves strictly inside candidate_root.

    Rejects empty paths, absolute paths, ./ prefixes, and .. traversal components.
    """
    if not relative:
        return False
    if relative.startswith(("/", "\\", "./")) or ":" in relative.split("/", 1)[0]:
        return False
    parts = PurePosixPath(relative).parts
    if ".." in parts or "." in parts:
        return False
    try:
        resolved = (candidate_root / relative).resolve()
        candidate_resolved = candidate_root.resolve()
        return str(resolved).startswith(str(candidate_resolved))
    except (OSError, ValueError):
        return False


def _parse_status_short(status_text: str) -> set[str]:
    """Parse git status --short output into relative paths."""
    paths: set[str] = set()
    for raw in status_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if " -> " in line:
            _, _, new_path = line.partition(" -> ")
            line = new_path.strip()
        elif len(line) >= 2:
            line = line[2:].strip()
        if line and not line.startswith(("/", "\\")):
            paths.add(line)
    return paths


def compute_modified_paths(
    candidate_root: Path,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return sanitized, deduplicated relative paths modified during execution."""
    before_paths = _parse_status_short(str(before.get("working_tree", "")))
    after_paths = _parse_status_short(str(after.get("working_tree", "")))
    combined = before_paths | after_paths
    contained = [
        p for p in sorted(combined) if is_path_within_candidate(candidate_root, p)
    ]
    return tuple(contained)


def build_executor_environment(
    request: ExecutionRequest,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal executor environment with request env_projection."""
    env = build_minimal_executor_env(
        base,
        explicit_projection=request.env_projection,
        candidate_root=request.candidate_root,
    )
    env.setdefault("NO_COLOR", "1")
    return env


@dataclass(frozen=True, slots=True)
class ContainmentPolicy:
    """Policy configuring containment boundary for an execution."""

    mode: ContainmentMode = ContainmentMode.BWRAP
    network_mode: NetworkMode = NetworkMode.NONE
    writable_paths: tuple[Path, ...] = ()
    readonly_paths: tuple[Path, ...] = ()
    forbidden_paths: tuple[Path, ...] = ()
    timeout_seconds: float = 300.0
    max_output_bytes: int = 262_144
    allow_uncontained_fallback: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.mode, str):
            object.__setattr__(self, "mode", ContainmentMode(self.mode))
        if isinstance(self.network_mode, str):
            object.__setattr__(self, "network_mode", NetworkMode(self.network_mode))
        object.__setattr__(
            self,
            "writable_paths",
            tuple(Path(p) for p in self.writable_paths),
        )
        object.__setattr__(
            self,
            "readonly_paths",
            tuple(Path(p) for p in self.readonly_paths),
        )
        object.__setattr__(
            self,
            "forbidden_paths",
            tuple(Path(p) for p in self.forbidden_paths),
        )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be >= 1")


@dataclass(frozen=True, slots=True)
class ContainmentDoctorReport:
    """Health and readiness assessment of host containment subsystems."""

    bwrap_path: str | None
    userns_available: bool
    healthy: bool
    reason: str | None = None


def probe_bwrap_containment() -> ContainmentDoctorReport:
    """Probe host for bwrap executable and functional Linux user namespaces."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return ContainmentDoctorReport(
            bwrap_path=None,
            userns_available=False,
            healthy=False,
            reason="bwrap executable not found in PATH",
        )

    probe_cmd = [
        bwrap,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--unshare-all",
        "/bin/true",
    ]
    try:
        res = subprocess.run(
            probe_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
        if res.returncode == 0:
            return ContainmentDoctorReport(
                bwrap_path=bwrap,
                userns_available=True,
                healthy=True,
                reason=None,
            )
        err = res.stderr.decode("utf-8", errors="replace").strip()
        return ContainmentDoctorReport(
            bwrap_path=bwrap,
            userns_available=False,
            healthy=False,
            reason=f"bwrap userns probe failed with exit code {res.returncode}: {err}",
        )
    except subprocess.TimeoutExpired:
        return ContainmentDoctorReport(
            bwrap_path=bwrap,
            userns_available=False,
            healthy=False,
            reason="bwrap userns probe timed out after 5 seconds",
        )
    except Exception as exc:
        return ContainmentDoctorReport(
            bwrap_path=bwrap,
            userns_available=False,
            healthy=False,
            reason=f"bwrap userns probe raised exception: {exc}",
        )


def build_bwrap_argv(
    command: Sequence[str],
    *,
    candidate_root: Path,
    canonical_root: Path | None = None,
    policy: ContainmentPolicy,
    env: Mapping[str, str],
) -> list[str]:
    """Construct a bubblewrap invocation command vector."""
    bwrap_bin = shutil.which("bwrap") or "bwrap"
    argv = [
        bwrap_bin,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
    ]
    if policy.network_mode != NetworkMode.NONE:
        argv.append("--share-net")

    # Read-only root and essential system mounts
    argv.extend(["--ro-bind", "/", "/"])
    argv.extend(["--dev", "/dev"])
    argv.extend(["--proc", "/proc"])
    argv.extend(["--tmpfs", "/tmp"])

    # Canonical root masking (must be masked AFTER --ro-bind / / and --tmpfs /tmp)
    if canonical_root is not None:
        c_root = Path(canonical_root).resolve()
        cand_root = Path(candidate_root).resolve()
        if c_root != cand_root and canonical_root.exists():
            if canonical_root.is_dir():
                argv.extend(["--tmpfs", str(canonical_root)])
            else:
                argv.extend(["--ro-bind", "/dev/null", str(canonical_root)])

    # Forbidden paths masking
    for fp in policy.forbidden_paths:
        if fp.exists():
            if fp.is_dir():
                argv.extend(["--tmpfs", str(fp)])
            else:
                argv.extend(["--ro-bind", "/dev/null", str(fp)])

    # Readonly paths
    for rp in policy.readonly_paths:
        if rp.exists():
            argv.extend(["--ro-bind", str(rp), str(rp)])

    # Writable bind mounts: candidate root and explicitly allowed writable paths
    argv.extend(["--bind", str(candidate_root), str(candidate_root)])
    for wp in policy.writable_paths:
        if wp.resolve() != Path(candidate_root).resolve() and wp.exists():
            argv.extend(["--bind", str(wp), str(wp)])

    # Change to candidate root
    argv.extend(["--chdir", str(candidate_root)])

    # Sanitized environment
    argv.append("--clearenv")
    for k in sorted(env.keys()):
        argv.extend(["--setenv", str(k), str(env[k])])

    argv.extend(list(command))
    return argv


@dataclass(frozen=True, slots=True)
class ContainedExecutionResult:
    """Outcome of a contained process execution."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    containment_profile: str
    orphan_cleanup_ran: bool


class ContainedProcessRunner:
    """Process runner enforcing containment policies and process-tree lifecycle cleanup."""

    def __init__(self, doctor_report: ContainmentDoctorReport | None = None) -> None:
        self._doctor_report = doctor_report

    def _get_doctor_report(self) -> ContainmentDoctorReport:
        if self._doctor_report is not None:
            return self._doctor_report
        return probe_bwrap_containment()

    def run(
        self,
        command: Sequence[str],
        *,
        candidate_root: Path,
        canonical_root: Path | None = None,
        policy: ContainmentPolicy | None = None,
        env: Mapping[str, str] | None = None,
        stdin_bytes: bytes | None = None,
    ) -> ContainedExecutionResult:
        """Synchronously execute command under ContainmentPolicy."""
        pol = policy or ContainmentPolicy()
        effective_env = dict(env or {})

        report = self._get_doctor_report()
        if pol.mode == ContainmentMode.BWRAP:
            if not report.healthy:
                if not pol.allow_uncontained_fallback:
                    raise ContainmentUnavailableError(
                        f"bwrap containment requested but unavailable: {report.reason}"
                    )
                use_bwrap = False
                containment_profile = "UNCONTAINED_TRUSTED_DEV"
            else:
                use_bwrap = True
                containment_profile = "bwrap"
        else:
            use_bwrap = False
            containment_profile = "UNCONTAINED_TRUSTED_DEV"

        if use_bwrap:
            argv = build_bwrap_argv(
                command,
                candidate_root=candidate_root,
                canonical_root=canonical_root,
                policy=pol,
                env=effective_env,
            )
            cwd = str(candidate_root)
            proc_env = None
        else:
            argv = list(command)
            cwd = str(candidate_root)
            proc_env = effective_env

        timed_out = False
        orphan_cleanup_ran = False
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=proc_env,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        pgid = proc.pid

        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=pol.timeout_seconds)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
            returncode = -signal.SIGKILL
        finally:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            orphan_cleanup_ran = True

        return ContainedExecutionResult(
            returncode=returncode,
            stdout=out[: pol.max_output_bytes],
            stderr=err[: pol.max_output_bytes],
            timed_out=timed_out,
            containment_profile=containment_profile,
            orphan_cleanup_ran=orphan_cleanup_ran,
        )

    async def run_async(
        self,
        command: Sequence[str],
        *,
        candidate_root: Path,
        canonical_root: Path | None = None,
        policy: ContainmentPolicy | None = None,
        env: Mapping[str, str] | None = None,
        stdin_bytes: bytes | None = None,
    ) -> ContainedExecutionResult:
        """Asynchronously execute command under ContainmentPolicy."""
        return await asyncio.to_thread(
            self.run,
            command,
            candidate_root=candidate_root,
            canonical_root=canonical_root,
            policy=policy,
            env=env,
            stdin_bytes=stdin_bytes,
        )
