"""Environment & Prerequisites Diagnostic Engine (Conrad Doctor).

Phase 3 Operator Experience (M3.4 / A3.4.1):
100% read-only diagnostic engine probing Python runtime, Git capabilities,
state directory writability/permissions, SQLite integrity, project configuration,
containment subsystems, executor readiness, and transport availability.

Zero fastmcp imports at module top-level.
Zero modifications to files, repositories, or databases.
Recursive secret and credential redaction on all diagnostic outputs.
Every FAIL and WARN check provides an explicit, actionable remediation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
import importlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
from typing import Any

from .cli import redact_cli_data
from .containment import ContainmentDoctorReport, probe_bwrap_containment
from .transports import LocalHttpTransportAdapter, TransportConfig, TransportMode


class DoctorSeverity(str, Enum):
    """Severity classification for a diagnostic check result."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class DoctorCheckResult:
    """Individual diagnostic check outcome with actionable remediation."""

    check_id: str
    scope: str
    severity: DoctorSeverity
    summary: str
    remediation: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize check result to JSON-safe dictionary with redacted secrets."""
        raw: dict[str, Any] = {
            "check_id": self.check_id,
            "scope": self.scope,
            "severity": self.severity.value,
            "summary": self.summary,
            "remediation": self.remediation,
            "details": dict(self.details),
            "read_only": self.read_only,
        }
        return redact_cli_data(raw)  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Consolidated diagnostic report summarizing environment health."""

    overall_healthy: bool
    status: str  # "pass", "warn", "fail"
    checks: tuple[DoctorCheckResult, ...]
    summary: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Serialize complete report to JSON-safe dictionary with redacted secrets."""
        raw: dict[str, Any] = {
            "overall_healthy": self.overall_healthy,
            "status": self.status,
            "summary": dict(self.summary),
            "checks": [c.to_dict() for c in self.checks],
        }
        return redact_cli_data(raw)  # type: ignore[no-any-return]


def _check_python_runtime() -> DoctorCheckResult:
    """Check Python version >= 3.11 and required library availability."""
    version_info = sys.version_info
    major = getattr(version_info, "major", version_info[0])
    minor = getattr(version_info, "minor", version_info[1])
    micro = getattr(version_info, "micro", version_info[2])
    ver_str = f"{major}.{minor}.{micro}"
    details: dict[str, Any] = {"version": ver_str, "executable": sys.executable}

    if (major, minor) < (3, 11):
        return DoctorCheckResult(
            check_id="python_runtime",
            scope="runtime",
            severity=DoctorSeverity.FAIL,
            summary=f"Python version {ver_str} is unsupported; Conrad requires Python >= 3.11",
            remediation="Upgrade Python to version 3.11 or later.",
            details=details,
        )

    required_modules = ["pydantic", "fastmcp"]
    missing: list[str] = []
    for mod in required_modules:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)

    if missing:
        return DoctorCheckResult(
            check_id="python_runtime",
            scope="runtime",
            severity=DoctorSeverity.FAIL,
            summary=f"Missing required Python dependencies: {', '.join(missing)}",
            remediation=f"Install dependencies via: pip install {' '.join(repr(m) for m in missing)}",
            details=details,
        )

    return DoctorCheckResult(
        check_id="python_runtime",
        scope="runtime",
        severity=DoctorSeverity.PASS,
        summary=f"Python {ver_str} runtime and required dependencies are available",
        details=details,
    )


def _check_git_runtime() -> DoctorCheckResult:
    """Check Git availability and execution readiness."""
    git_bin = shutil.which("git")
    if not git_bin:
        return DoctorCheckResult(
            check_id="git_runtime",
            scope="runtime",
            severity=DoctorSeverity.FAIL,
            summary="Git executable was not found in PATH",
            remediation="Install Git via system package manager (e.g. sudo apt-get install -y git).",
            details={"git_binary": None},
        )

    try:
        proc = subprocess.run(
            [git_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode != 0:
            return DoctorCheckResult(
                check_id="git_runtime",
                scope="runtime",
                severity=DoctorSeverity.FAIL,
                summary=f"Git check failed with exit code {proc.returncode}: {proc.stderr.strip()}",
                remediation="Ensure Git is properly installed and executable on your system.",
                details={"git_binary": git_bin, "returncode": proc.returncode},
            )
        git_ver = proc.stdout.strip()
        return DoctorCheckResult(
            check_id="git_runtime",
            scope="runtime",
            severity=DoctorSeverity.PASS,
            summary=f"Git runtime is available ({git_ver})",
            details={"git_binary": git_bin, "version": git_ver},
        )
    except Exception as exc:
        return DoctorCheckResult(
            check_id="git_runtime",
            scope="runtime",
            severity=DoctorSeverity.FAIL,
            summary=f"Failed to execute git: {exc}",
            remediation="Verify Git permissions and executable integrity.",
            details={"git_binary": git_bin, "error": str(exc)},
        )


def _check_state_directory(state_dir: Path | None) -> DoctorCheckResult:
    """Check state directory existence, writability, and private 0700 permissions."""
    if state_dir is None:
        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.FAIL,
            summary="State directory path is not specified",
            remediation="Specify a valid state directory path using --state-dir.",
        )

    resolved = Path(state_dir).expanduser().resolve()
    details: dict[str, Any] = {"path": str(resolved)}

    if not resolved.exists():
        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.FAIL,
            summary=f"State directory does not exist: {resolved}",
            remediation=f"Initialize state directory via: conrad init --state-dir {resolved}",
            details=details,
        )

    if not resolved.is_dir():
        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.FAIL,
            summary=f"State path exists but is not a directory: {resolved}",
            remediation=f"Remove file and recreate directory: rm {resolved} && mkdir -m 700 {resolved}",
            details=details,
        )

    if not os.access(resolved, os.W_OK):
        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.FAIL,
            summary=f"State directory is not writable by current user: {resolved}",
            remediation=f"Grant write permissions: chmod u+w {resolved}",
            details=details,
        )

    try:
        st = resolved.stat()
        mode = stat.S_IMODE(st.st_mode)
        octal_mode = oct(mode)
        details["mode"] = octal_mode

        if (mode & 0o077) != 0:
            return DoctorCheckResult(
                check_id="state_directory",
                scope="filesystem",
                severity=DoctorSeverity.WARN,
                summary=f"State directory permissions ({octal_mode}) are too permissive (expected 0700)",
                remediation=f"Restrict state directory permissions: chmod 700 {resolved}",
                details=details,
            )

        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.PASS,
            summary=f"State directory is writable with private permissions ({octal_mode})",
            details=details,
        )
    except Exception as exc:
        return DoctorCheckResult(
            check_id="state_directory",
            scope="filesystem",
            severity=DoctorSeverity.FAIL,
            summary=f"Failed to stat state directory: {exc}",
            remediation=f"Check permissions and ownership of {resolved}",
            details=details,
        )


def _check_sqlite_state_db(state_dir: Path | None) -> DoctorCheckResult:
    """Check integrity of SQLite state database read-only via PRAGMA integrity_check."""
    if state_dir is None:
        return DoctorCheckResult(
            check_id="sqlite_state_db",
            scope="database",
            severity=DoctorSeverity.SKIP,
            summary="State directory not specified; skipping SQLite check",
        )

    resolved_dir = Path(state_dir).expanduser().resolve()
    if not resolved_dir.is_dir():
        return DoctorCheckResult(
            check_id="sqlite_state_db",
            scope="database",
            severity=DoctorSeverity.SKIP,
            summary="State directory not present; skipping SQLite check",
        )

    candidates = [
        resolved_dir / "state.db",
        resolved_dir / "conrad.db",
        resolved_dir / "bridge.db",
    ]
    existing = [c for c in candidates if c.is_file()]

    if not existing:
        return DoctorCheckResult(
            check_id="sqlite_state_db",
            scope="database",
            severity=DoctorSeverity.PASS,
            summary="No SQLite state database present (fresh installation)",
            details={"state_dir": str(resolved_dir)},
        )

    for db_path in existing:
        details: dict[str, Any] = {"path": str(db_path)}
        try:
            # Read-only URI connection
            uri = f"file:{db_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                rows = cur.fetchall()
                if not rows or rows[0][0] != "ok":
                    first_err = rows[0][0] if rows else "no output"
                    return DoctorCheckResult(
                        check_id="sqlite_state_db",
                        scope="database",
                        severity=DoctorSeverity.FAIL,
                        summary=f"SQLite database {db_path.name} failed integrity check: {first_err}",
                        remediation=(
                            f"Restore corrupted database from backup or re-initialize: "
                            f"cp {db_path}.bak {db_path} or conrad init --allow-existing-with-backup"
                        ),
                        details={"path": str(db_path), "integrity_results": [r[0] for r in rows]},
                    )
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            return DoctorCheckResult(
                check_id="sqlite_state_db",
                scope="database",
                severity=DoctorSeverity.FAIL,
                summary=f"SQLite database {db_path.name} is corrupt or unreadable: {exc}",
                remediation=(
                    f"Restore database from backup or re-initialize: "
                    f"conrad init --allow-existing-with-backup"
                ),
                details={"path": str(db_path), "error": str(exc)},
            )
        except Exception as exc:
            return DoctorCheckResult(
                check_id="sqlite_state_db",
                scope="database",
                severity=DoctorSeverity.FAIL,
                summary=f"Unexpected error opening SQLite database {db_path.name}: {exc}",
                remediation=f"Verify file permissions and read access on {db_path}",
                details={"path": str(db_path), "error": str(exc)},
            )

    return DoctorCheckResult(
        check_id="sqlite_state_db",
        scope="database",
        severity=DoctorSeverity.PASS,
        summary=f"SQLite database passed integrity check ({', '.join(d.name for d in existing)})",
        details={"databases": [str(d) for d in existing]},
    )


def _check_project_config(config_path: Path | None) -> DoctorCheckResult:
    """Validate project registry configuration read-only."""
    if config_path is None:
        return DoctorCheckResult(
            check_id="project_config",
            scope="config",
            severity=DoctorSeverity.FAIL,
            summary="Projects configuration file path not specified",
            remediation="Specify configuration path via --config or create one with conrad init.",
        )

    resolved = Path(config_path).expanduser().resolve()
    details: dict[str, Any] = {"path": str(resolved)}

    if not resolved.exists():
        return DoctorCheckResult(
            check_id="project_config",
            scope="config",
            severity=DoctorSeverity.FAIL,
            summary=f"Projects configuration file not found: {resolved}",
            remediation=f"Initialize configuration via: conrad init --config {resolved}",
            details=details,
        )

    from .project_registration import validate_projects_config

    try:
        report = validate_projects_config(resolved)
        if not report.get("valid", False):
            errors = report.get("errors", [])
            err_msg = "; ".join(str(e) for e in errors) or "Validation failed"
            return DoctorCheckResult(
                check_id="project_config",
                scope="config",
                severity=DoctorSeverity.FAIL,
                summary=f"Configuration validation failed: {err_msg}",
                remediation=f"Validate and repair configuration via: conrad project validate --config {resolved}",
                details={"path": str(resolved), "errors": errors, "warnings": report.get("warnings", [])},
            )

        projects = report.get("projects", [])
        return DoctorCheckResult(
            check_id="project_config",
            scope="config",
            severity=DoctorSeverity.PASS,
            summary=f"Projects configuration is valid ({len(projects)} registered project(s))",
            details={"path": str(resolved), "project_count": len(projects)},
        )
    except Exception as exc:
        return DoctorCheckResult(
            check_id="project_config",
            scope="config",
            severity=DoctorSeverity.FAIL,
            summary=f"Failed to validate projects configuration: {exc}",
            remediation=f"Inspect and fix syntax in {resolved} or run conrad project validate --config {resolved}",
            details={"path": str(resolved), "error": str(exc)},
        )


def _check_containment_bwrap(
    profile: str | None,
    bwrap_probe_fn: Callable[[], ContainmentDoctorReport] | None,
) -> DoctorCheckResult:
    """Check Bubblewrap and user namespace containment readiness."""
    report = bwrap_probe_fn() if bwrap_probe_fn is not None else probe_bwrap_containment()
    details: dict[str, Any] = {
        "bwrap_path": report.bwrap_path,
        "userns_available": report.userns_available,
        "healthy": report.healthy,
        "reason": report.reason,
    }

    if report.healthy:
        return DoctorCheckResult(
            check_id="containment_bwrap",
            scope="containment",
            severity=DoctorSeverity.PASS,
            summary=f"Bubblewrap containment is operational ({report.bwrap_path})",
            details=details,
        )

    is_readonly = bool(profile and str(profile).lower() in ("readonly", "read-only"))
    remediation = (
        "Install bubblewrap and enable user namespaces: "
        "sudo apt-get install -y bubblewrap && sudo sysctl -w kernel.unprivileged_userns_clone=1"
    )

    if is_readonly:
        return DoctorCheckResult(
            check_id="containment_bwrap",
            scope="containment",
            severity=DoctorSeverity.WARN,
            summary=f"Bubblewrap containment unavailable: {report.reason} (non-blocking for readonly profile)",
            remediation=remediation,
            details=details,
        )

    return DoctorCheckResult(
        check_id="containment_bwrap",
        scope="containment",
        severity=DoctorSeverity.FAIL,
        summary=f"Bubblewrap containment required but unavailable: {report.reason}",
        remediation=remediation,
        details=details,
    )


def _check_executor_readiness(
    executor_kind: str | None,
    executor_health_fn: Callable[[], tuple[bool, str | None]] | None,
) -> DoctorCheckResult:
    """Check executor availability, skipping unselected optional runtimes."""
    if executor_health_fn is not None:
        try:
            ok, reason = executor_health_fn()
            details = {"executor": executor_kind or "custom", "healthy": ok, "reason": reason}
            if ok:
                return DoctorCheckResult(
                    check_id="executor_readiness",
                    scope="executor",
                    severity=DoctorSeverity.PASS,
                    summary=f"Executor '{executor_kind or 'custom'}' is operational",
                    details=details,
                )
            return DoctorCheckResult(
                check_id="executor_readiness",
                scope="executor",
                severity=DoctorSeverity.FAIL,
                summary=f"Executor '{executor_kind or 'custom'}' check failed: {reason}",
                remediation=f"Ensure executor runtime prerequisites are running: {reason or 'check executor setup'}",
                details=details,
            )
        except Exception as exc:
            return DoctorCheckResult(
                check_id="executor_readiness",
                scope="executor",
                severity=DoctorSeverity.FAIL,
                summary=f"Executor health probe raised exception: {exc}",
                remediation="Ensure executor service is reachable and configured properly.",
                details={"error": str(exc)},
            )

    target_kind = executor_kind.lower() if executor_kind else "claude"

    if target_kind == "openhands":
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return DoctorCheckResult(
                check_id="executor_readiness",
                scope="executor",
                severity=DoctorSeverity.FAIL,
                summary="Docker binary not found in PATH for OpenHands executor",
                remediation="Install Docker and ensure the daemon is running: sudo systemctl start docker",
                details={"docker_binary": None},
            )
        return DoctorCheckResult(
            check_id="executor_readiness",
            scope="executor",
            severity=DoctorSeverity.PASS,
            summary=f"Docker is available at {docker_bin} for OpenHands executor",
            details={"docker_binary": docker_bin},
        )

    if target_kind == "claude":
        claude_bin = shutil.which("claude")
        if claude_bin:
            return DoctorCheckResult(
                check_id="executor_readiness",
                scope="executor",
                severity=DoctorSeverity.PASS,
                summary=f"Claude Code CLI is available at {claude_bin}",
                details={"claude_binary": claude_bin},
            )

        if executor_kind is not None:
            return DoctorCheckResult(
                check_id="executor_readiness",
                scope="executor",
                severity=DoctorSeverity.FAIL,
                summary="Claude Code CLI ('claude') not found in PATH",
                remediation="Install Claude Code CLI via: npm install -g @anthropic-ai/claude-code",
                details={"claude_binary": None},
            )

        # Optional warning if not explicitly requested
        return DoctorCheckResult(
            check_id="executor_readiness",
            scope="executor",
            severity=DoctorSeverity.WARN,
            summary="Claude Code CLI ('claude') not found in PATH (optional for read-only operations)",
            remediation="Install Claude Code CLI via: npm install -g @anthropic-ai/claude-code",
            details={"claude_binary": None},
        )

    return DoctorCheckResult(
        check_id="executor_readiness",
        scope="executor",
        severity=DoctorSeverity.SKIP,
        summary=f"Readiness check skipped for executor '{executor_kind}'",
        details={"executor": executor_kind},
    )


def _check_transport_readiness(
    transport_mode: TransportMode,
    host: str,
    port: int,
    port_probe_fn: Callable[[str, int], None] | None,
) -> DoctorCheckResult:
    """Check transport surface readiness and port binding availability."""
    if transport_mode == TransportMode.STDIO:
        return DoctorCheckResult(
            check_id="transport_readiness",
            scope="transport",
            severity=DoctorSeverity.PASS,
            summary="STDIO transport is operational (standard I/O streams)",
            details={"mode": "stdio"},
        )

    if transport_mode == TransportMode.LOCAL_HTTP:
        details: dict[str, Any] = {"mode": "http", "host": host, "port": port}
        if port_probe_fn is not None:
            try:
                port_probe_fn(host, port)
                return DoctorCheckResult(
                    check_id="transport_readiness",
                    scope="transport",
                    severity=DoctorSeverity.PASS,
                    summary=f"Port {port} on {host} is available for HTTP transport",
                    details=details,
                )
            except OSError as exc:
                return DoctorCheckResult(
                    check_id="transport_readiness",
                    scope="transport",
                    severity=DoctorSeverity.FAIL,
                    summary=f"Port conflict on {host}:{port}: {exc}",
                    remediation=f"Stop service occupying port {port} or configure a different port using --port.",
                    details=details,
                )
            except Exception as exc:
                return DoctorCheckResult(
                    check_id="transport_readiness",
                    scope="transport",
                    severity=DoctorSeverity.FAIL,
                    summary=f"Failed to probe port {host}:{port}: {exc}",
                    remediation=f"Check network configuration and port availability for {host}:{port}.",
                    details=details,
                )

        try:
            cfg = TransportConfig(mode=TransportMode.LOCAL_HTTP, host=host, port=port)
            adapter = LocalHttpTransportAdapter(cfg)
            if adapter.probe_bind_available():
                return DoctorCheckResult(
                    check_id="transport_readiness",
                    scope="transport",
                    severity=DoctorSeverity.PASS,
                    summary=f"Loopback port {host}:{port} is available for HTTP transport",
                    details=details,
                )
            return DoctorCheckResult(
                check_id="transport_readiness",
                scope="transport",
                severity=DoctorSeverity.FAIL,
                summary=f"Port {port} on {host} is already in use",
                remediation=f"Stop conflicting service or choose another port with --port.",
                details=details,
            )
        except Exception as exc:
            return DoctorCheckResult(
                check_id="transport_readiness",
                scope="transport",
                severity=DoctorSeverity.FAIL,
                summary=f"Error checking HTTP transport binding: {exc}",
                remediation=f"Verify host address {host} and port {port}.",
                details={"error": str(exc)},
            )

    if transport_mode == TransportMode.NGROK_MTLS:
        ca_path_str = os.environ.get("NGROK_MTLS_CA_CERT_PATH")
        token_str = os.environ.get("NGROK_AUTHTOKEN")
        ca_ok = bool(ca_path_str and Path(ca_path_str).is_file())
        token_ok = bool(token_str and len(token_str.strip()) > 0)
        details = {
            "mode": "ngrok_mtls",
            "ca_cert_configured": ca_ok,
            "token_configured": token_ok,
        }

        if not ca_ok:
            return DoctorCheckResult(
                check_id="transport_readiness",
                scope="transport",
                severity=DoctorSeverity.FAIL,
                summary="Ngrok mTLS CA certificate missing or file not found",
                remediation="Set NGROK_MTLS_CA_CERT_PATH environment variable to a valid CA certificate path.",
                details=details,
            )

        if not token_ok:
            return DoctorCheckResult(
                check_id="transport_readiness",
                scope="transport",
                severity=DoctorSeverity.FAIL,
                summary="Ngrok authtoken is not configured",
                remediation="Configure ngrok authtoken via NGROK_AUTHTOKEN environment variable or ngrok config.",
                details=details,
            )

        return DoctorCheckResult(
            check_id="transport_readiness",
            scope="transport",
            severity=DoctorSeverity.PASS,
            summary="Ngrok mTLS transport configuration is valid",
            details=details,
        )

    return DoctorCheckResult(
        check_id="transport_readiness",
        scope="transport",
        severity=DoctorSeverity.SKIP,
        summary=f"Readiness check skipped for transport mode '{transport_mode.value}'",
        details={"mode": transport_mode.value},
    )


def run_conrad_doctor(
    *,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    transport_mode: TransportMode = TransportMode.STDIO,
    host: str = "127.0.0.1",
    port: int = 8000,
    profile: str | None = None,
    executor_kind: str | None = None,
    bwrap_probe_fn: Callable[[], ContainmentDoctorReport] | None = None,
    port_probe_fn: Callable[[str, int], None] | None = None,
    executor_health_fn: Callable[[], tuple[bool, str | None]] | None = None,
) -> DoctorReport:
    """Run full suite of read-only diagnostic checks and return consolidated report."""
    checks: list[DoctorCheckResult] = [
        _check_python_runtime(),
        _check_git_runtime(),
        _check_state_directory(state_dir),
        _check_sqlite_state_db(state_dir),
        _check_project_config(config_path),
        _check_containment_bwrap(profile, bwrap_probe_fn),
        _check_executor_readiness(executor_kind, executor_health_fn),
        _check_transport_readiness(transport_mode, host, port, port_probe_fn),
    ]

    summary: dict[str, int] = {
        DoctorSeverity.PASS.value: 0,
        DoctorSeverity.WARN.value: 0,
        DoctorSeverity.FAIL.value: 0,
        DoctorSeverity.SKIP.value: 0,
    }
    for c in checks:
        summary[c.severity.value] = summary.get(c.severity.value, 0) + 1

    overall_healthy = summary[DoctorSeverity.FAIL.value] == 0
    if summary[DoctorSeverity.FAIL.value] > 0:
        status = "fail"
    elif summary[DoctorSeverity.WARN.value] > 0:
        status = "warn"
    else:
        status = "pass"

    return DoctorReport(
        overall_healthy=overall_healthy,
        status=status,
        checks=tuple(checks),
        summary=summary,
    )


__all__ = [
    "DoctorCheckResult",
    "DoctorReport",
    "DoctorSeverity",
    "run_conrad_doctor",
]
