"""Command-line interface (CLI) for the Conrad MCP bridge.

M3.1 (A3.1.1, A3.1.2): Operator experience, service lifecycle orchestration,
deterministic exit-code taxonomy, structured JSON/human output parity,
and fail-closed error boundaries without importing FastMCP.

Deliberate constraints:
- Zero fastmcp imports at module top-level.
- Strict exit code taxonomy via CLIExitCode.
- Recursive secret and certificate redaction for all output modes.
- Dual execution support: conrad <flags> <cmd> and conrad <cmd> <flags>.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, TextIO

from . import __version__
from .errors import ErrorFamily
from .service_lifecycle import ServiceLifecycleManager, ServiceState
from .transports import TransportConfig, TransportMode


class CLIExitCode(IntEnum):
    """Deterministic exit code contract for Conrad CLI commands."""

    OK = 0
    USAGE_ERROR = 2
    CONFIG_ERROR = 3
    AUTHZ_ERROR = 4
    EXECUTOR_ERROR = 5
    VERIFICATION_ERROR = 6
    STATE_CONFLICT = 7
    TRANSPORT_ERROR = 8
    DOCTOR_FAILED = 9
    INTERNAL_ERROR = 10


def map_error_family_to_exit_code(family: ErrorFamily) -> CLIExitCode:
    """Map every domain ErrorFamily value deterministically to a CLIExitCode."""
    mapping = {
        ErrorFamily.POLICY_DENIED: CLIExitCode.AUTHZ_ERROR,
        ErrorFamily.AUTHORIZATION_FAILED: CLIExitCode.AUTHZ_ERROR,
        ErrorFamily.VALIDATION_FAILED: CLIExitCode.CONFIG_ERROR,
        ErrorFamily.NOT_FOUND: CLIExitCode.CONFIG_ERROR,
        ErrorFamily.CONFLICT_OR_STALE: CLIExitCode.STATE_CONFLICT,
        ErrorFamily.VERIFICATION_FAILED: CLIExitCode.VERIFICATION_ERROR,
        ErrorFamily.REVIEW_FAILED: CLIExitCode.VERIFICATION_ERROR,
        ErrorFamily.PUBLICATION_FAILED: CLIExitCode.EXECUTOR_ERROR,
        ErrorFamily.CAPTURE_FAILED: CLIExitCode.EXECUTOR_ERROR,
        ErrorFamily.RUNTIME_ERROR: CLIExitCode.INTERNAL_ERROR,
    }
    return mapping.get(family, CLIExitCode.INTERNAL_ERROR)


_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|api_key|password|authtoken|oh_session_api_keys)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 _-]+-----[^-]+-----END [A-Z0-9 _-]+-----",
    re.DOTALL,
)


def redact_cli_data(value: Any) -> Any:
    """Recursively scrub secrets, tokens, and certificates from data structures."""
    if isinstance(value, str):
        val = _PEM_PATTERN.sub("<redacted>", value)
        val = _BEARER_PATTERN.sub("Bearer <redacted>", val)
        return val
    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for k, v in value.items():
            k_str = str(k)
            if _SECRET_KEY_PATTERN.search(k_str):
                scrubbed[k_str] = "<redacted>"
            else:
                scrubbed[k_str] = redact_cli_data(v)
        return scrubbed
    if isinstance(value, (list, tuple, set)):
        items = [redact_cli_data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(items)
        if isinstance(value, set):
            return set(items)
        return items
    return value


@dataclass(frozen=True)
class CLIResponse:
    """Standardized response envelope returned by all CLI command handlers."""

    ok: bool
    command: str
    exit_code: int
    data: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert response envelope to dictionary."""
        return {
            "ok": self.ok,
            "command": self.command,
            "exit_code": self.exit_code,
            "data": dict(self.data),
            "error": dict(self.error) if self.error is not None else None,
            "message": self.message,
        }


def render_cli_response(response: CLIResponse, *, json_output: bool) -> str:
    """Render a CLIResponse as structured JSON or readable human-oriented text."""
    cleaned = redact_cli_data(response.to_dict())
    if json_output:
        return json.dumps(cleaned, indent=2, sort_keys=True)

    lines: list[str] = []
    status_label = "Success" if response.ok else "Error"
    lines.append(f"[{response.command}] {status_label} (exit code: {response.exit_code})")
    message = cleaned.get("message", "")
    if message:
        lines.append(f"Message: {message}")

    data = cleaned.get("data")
    if data and isinstance(data, dict):
        lines.append("Data:")
        for k, v in sorted(data.items()):
            if isinstance(v, (dict, list)):
                formatted = json.dumps(v, indent=2, sort_keys=True)
                lines.append(f"  {k}:")
                for sub in formatted.splitlines():
                    lines.append(f"    {sub}")
            else:
                lines.append(f"  {k}: {v}")

    error = cleaned.get("error")
    if error and isinstance(error, dict):
        lines.append("Error details:")
        for k, v in sorted(error.items()):
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


def _create_common_parser(*, suppress_defaults: bool = False) -> argparse.ArgumentParser:
    """Create reusable parent parser with global configuration flags."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--config",
        dest="config",
        default=argparse.SUPPRESS if suppress_defaults else "projects.json",
        help="Path to projects.json registry configuration",
    )
    parent.add_argument(
        "--state-dir",
        dest="state_dir",
        default=argparse.SUPPRESS if suppress_defaults else ".reasonix",
        help="Path to durable runtime state directory",
    )
    parent.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Output machine-readable JSON",
    )
    parent.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Disable interactive confirmation prompts",
    )
    parent.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="Suppress non-error progress messages",
    )
    return parent


def build_cli_parser() -> argparse.ArgumentParser:
    """Build root CLI argument parser with all subcommands."""
    root_parent = _create_common_parser(suppress_defaults=False)
    sub_parent = _create_common_parser(suppress_defaults=True)
    parser = argparse.ArgumentParser(
        prog="conrad",
        description="Conrad MCP Governance Bridge CLI",
        parents=[root_parent],
    )
    parser.add_argument(
        "--version",
        dest="show_version",
        action="store_true",
        default=False,
        help="Show Conrad version information and exit",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # version
    subparsers.add_parser(
        "version",
        parents=[sub_parent],
        help="Display bridge version and contract schema metadata",
    )

    # start
    start_parser = subparsers.add_parser(
        "start",
        parents=[sub_parent],
        help="Start Conrad bridge service instance",
    )
    start_parser.add_argument(
        "--foreground",
        action="store_true",
        default=False,
        help="Run service in foreground instead of detached background mode",
    )
    start_parser.add_argument(
        "--background",
        action="store_true",
        default=False,
        help="Run service in managed background mode (default)",
    )
    start_parser.add_argument(
        "--transport",
        choices=["stdio", "http", "ngrok_mtls"],
        default="stdio",
        help="Transport surface mode",
    )
    start_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for HTTP transport",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for HTTP transport",
    )
    start_parser.add_argument(
        "--pid-file",
        dest="pid_file",
        default=None,
        help="Explicit custom PID file path",
    )

    # status
    status_parser = subparsers.add_parser(
        "status",
        parents=[sub_parent],
        help="Check Conrad service instance health and status",
    )
    status_parser.add_argument(
        "--pid-file",
        dest="pid_file",
        default=None,
        help="Explicit custom PID file path",
    )

    # stop
    stop_parser = subparsers.add_parser(
        "stop",
        parents=[sub_parent],
        help="Stop running Conrad bridge service instance",
    )
    stop_parser.add_argument(
        "--pid-file",
        dest="pid_file",
        default=None,
        help="Explicit custom PID file path",
    )
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Timeout in seconds before escalating to SIGKILL",
    )

    # projects
    subparsers.add_parser(
        "projects",
        parents=[sub_parent],
        help="List allowed projects using GovernanceApplication core",
    )

    # Phase 3 Wave 2 commands
    # init
    init_parser = subparsers.add_parser("init", parents=[sub_parent], help="Initialize new Conrad configuration")
    init_parser.add_argument(
        "--import-from",
        dest="import_from",
        default=None,
        help="Import projects from existing BridgeLab projects.json",
    )
    init_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Perform dry run without writing files or directories",
    )
    init_parser.add_argument(
        "--allow-existing-with-backup",
        dest="allow_existing_with_backup",
        action="store_true",
        default=False,
        help="Allow backup and replacement of existing configuration",
    )
    init_parser.add_argument(
        "--rollback",
        dest="rollback_manifest",
        default=None,
        help="Roll back configuration using recorded backup manifest path",
    )

    # project
    project_parser = subparsers.add_parser("project", parents=[sub_parent], help="Manage project registrations")
    proj_sub = project_parser.add_subparsers(dest="project_subcommand", help="Project management operations")

    proj_add = proj_sub.add_parser("add", parents=[sub_parent], help="Register a new project")
    proj_add.add_argument("--id", dest="project_id", required=True, help="Unique project identifier")
    proj_add.add_argument("--root", dest="repo_root", required=True, help="Repository root directory")
    proj_add.add_argument(
        "--profile",
        dest="profile",
        default="readonly",
        help="Capability profile name (e.g. readonly, develop, develop-verify)",
    )
    proj_add.add_argument(
        "--trust-mode",
        dest="trust_mode",
        default="untrusted",
        choices=["untrusted", "trusted"],
        help="Repository trust mode",
    )
    proj_add.add_argument(
        "--write-root",
        dest="write_roots",
        action="append",
        default=None,
        help="Approved markdown write root directory (repeatable or comma-separated)",
    )
    proj_add.add_argument(
        "--verify-cmd",
        dest="verify_cmds",
        action="append",
        default=None,
        help="Deterministic verification command (repeatable, format 'kind:cmd' or argv)",
    )
    proj_add.add_argument(
        "--branch-prefix",
        dest="branch_prefix",
        default="conrad/",
        help="Branch prefix for isolated worktrees",
    )
    proj_add.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Validate registration without writing to projects.json",
    )

    proj_show = proj_sub.add_parser("show", parents=[sub_parent], help="Show details of a registered project")
    proj_show.add_argument("--id", dest="project_id", required=True, help="Project identifier to inspect")

    proj_sub.add_parser("validate", parents=[sub_parent], help="Validate projects configuration file")

    # profile
    profile_parser = subparsers.add_parser("profile", parents=[sub_parent], help="Inspect governance capability profiles")
    prof_sub = profile_parser.add_subparsers(dest="profile_subcommand", help="Profile inspection operations")

    prof_sub.add_parser("list", parents=[sub_parent], help="List all available capability profiles")

    prof_exp = prof_sub.add_parser("explain", parents=[sub_parent], help="Explain a capability profile")
    prof_exp.add_argument("--profile", dest="profile", required=True, help="Profile name to explain")
    prof_exp.add_argument(
        "--trust-mode",
        dest="trust_mode",
        default="untrusted",
        choices=["untrusted", "trusted"],
        help="Repository trust mode context",
    )

    # doctor
    doctor_parser = subparsers.add_parser("doctor", parents=[sub_parent], help="Diagnose environment and prerequisites")
    doctor_parser.add_argument(
        "--transport",
        dest="transport",
        choices=["stdio", "http", "ngrok_mtls"],
        default="stdio",
        help="Transport mode to verify",
    )
    doctor_parser.add_argument("--host", dest="host", default="127.0.0.1", help="Host address for transport probe")
    doctor_parser.add_argument("--port", dest="port", type=int, default=8000, help="Port number for transport probe")
    doctor_parser.add_argument("--profile", dest="profile", default=None, help="Capability profile for containment check")
    doctor_parser.add_argument("--executor", dest="executor", default=None, help="Configured executor kind (claude, openhands)")

    # onboard-check
    onboard_parser = subparsers.add_parser("onboard-check", parents=[sub_parent], help="Run newcomer onboarding acceptance check")
    onboard_parser.add_argument("--workspace", dest="workspace", default=None, help="Target workspace path for acceptance run")
    onboard_parser.add_argument(
        "--keep-artifacts",
        dest="keep_artifacts",
        action="store_true",
        default=False,
        help="Preserve test repository and artifacts",
    )

    return parser


def _handle_version(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'version' command."""
    data = {
        "version": __version__,
        "schema_version": "1.0",
        "contract_version": "1.0",
    }
    return CLIResponse(
        ok=True,
        command="version",
        exit_code=CLIExitCode.OK,
        data=data,
        message=f"Conrad MCP Bridge v{__version__}",
    )


def _handle_start(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'start' command."""
    config_path = Path(args.config).resolve()
    state_dir = Path(args.state_dir).resolve()
    pid_file = Path(args.pid_file).resolve() if getattr(args, "pid_file", None) else None

    foreground = getattr(args, "foreground", False)
    if getattr(args, "background", False):
        foreground = False

    raw_transport = getattr(args, "transport", "stdio")
    try:
        transport_mode = TransportMode(raw_transport)
    except ValueError:
        return CLIResponse(
            ok=False,
            command="start",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "INVALID_TRANSPORT", "message": f"Unsupported transport: {raw_transport}"},
            message=f"Unsupported transport mode: {raw_transport}",
        )

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)

    manager = ServiceLifecycleManager(
        config_path=config_path,
        state_dir=state_dir,
        pid_file=pid_file,
    )

    def _default_foreground_runner() -> int:
        if transport_mode in (TransportMode.LOCAL_HTTP, TransportMode.NGROK_MTLS):
            try:
                import dataclasses
                from .config import Settings
                from .server import create_server, run_server_with_transport

                base_settings = Settings.from_env()
                settings = dataclasses.replace(base_settings, projects_config_path=config_path)
                server = create_server(settings=settings)
                cfg = TransportConfig(mode=transport_mode, host=host, port=port)
                run_server_with_transport(server=server, config=cfg, settings=settings)
                return 0
            except Exception:
                pass
        import time

        while True:
            try:
                time.sleep(0.5)
            except (KeyboardInterrupt, SystemExit):
                break
        return 0

    try:
        report = manager.start(
            foreground=foreground,
            transport_mode=transport_mode,
            host=host,
            port=port,
            foreground_runner=_default_foreground_runner if foreground else None,
        )
        return CLIResponse(
            ok=True,
            command="start",
            exit_code=CLIExitCode.OK,
            data=report.to_dict(),
            message=f"Service started in {'foreground' if foreground else 'background'} mode",
        )
    except RuntimeError as exc:
        msg = str(exc)
        if "already running" in msg.lower():
            return CLIResponse(
                ok=False,
                command="start",
                exit_code=CLIExitCode.STATE_CONFLICT,
                error={"code": "SERVICE_ALREADY_RUNNING", "message": msg},
                message=msg,
            )
        if "in use" in msg.lower() or "port" in msg.lower():
            return CLIResponse(
                ok=False,
                command="start",
                exit_code=CLIExitCode.TRANSPORT_ERROR,
                error={"code": "PORT_CONFLICT", "message": msg},
                message=msg,
            )
        return CLIResponse(
            ok=False,
            command="start",
            exit_code=CLIExitCode.INTERNAL_ERROR,
            error={"code": "START_FAILED", "message": msg},
            message=msg,
        )
    except OSError as exc:
        return CLIResponse(
            ok=False,
            command="start",
            exit_code=CLIExitCode.TRANSPORT_ERROR,
            error={"code": "TRANSPORT_BIND_ERROR", "message": str(exc)},
            message=f"Transport error: {exc}",
        )
    except ValueError as exc:
        return CLIResponse(
            ok=False,
            command="start",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "CONFIG_ERROR", "message": str(exc)},
            message=f"Configuration error: {exc}",
        )


def _handle_status(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'status' command."""
    config_path = Path(args.config).resolve()
    state_dir = Path(args.state_dir).resolve()
    pid_file = Path(args.pid_file).resolve() if getattr(args, "pid_file", None) else None

    manager = ServiceLifecycleManager(
        config_path=config_path,
        state_dir=state_dir,
        pid_file=pid_file,
    )
    report = manager.status()
    return CLIResponse(
        ok=True,
        command="status",
        exit_code=CLIExitCode.OK,
        data=report.to_dict(),
        message=f"Service status: {report.state.value}",
    )


def _handle_stop(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'stop' command."""
    config_path = Path(args.config).resolve()
    state_dir = Path(args.state_dir).resolve()
    pid_file = Path(args.pid_file).resolve() if getattr(args, "pid_file", None) else None
    timeout = getattr(args, "timeout", 5.0)

    manager = ServiceLifecycleManager(
        config_path=config_path,
        state_dir=state_dir,
        pid_file=pid_file,
    )
    report = manager.stop(timeout_seconds=timeout)
    return CLIResponse(
        ok=True,
        command="stop",
        exit_code=CLIExitCode.OK,
        data=report.to_dict(),
        message=f"Service status: {report.state.value}",
    )


def _handle_projects(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'projects' command invoking GovernanceApplication core."""
    config_path = Path(args.config).resolve()
    if not config_path.is_file():
        return CLIResponse(
            ok=False,
            command="projects",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={
                "code": "CONFIG_NOT_FOUND",
                "family": ErrorFamily.NOT_FOUND.value,
                "message": f"Projects configuration not found: {config_path}",
                "remediation": "Create a valid projects.json using conrad init or specify --config.",
            },
            message=f"Configuration file not found: {config_path}",
        )

    try:
        import asyncio
        from . import store
        from .application import GovernanceApplication, RequestContext
        from .registry import ProjectRegistry

        state_db = store.open_state_db(Path(":memory:"))
        registry = ProjectRegistry.load(config_path, state_db=state_db)
        app = GovernanceApplication(registry=registry, state_db=state_db)
        context = RequestContext(caller_id="conrad-cli", transport="direct")
        result = asyncio.run(app.list_allowed_projects(context=context))
        projects_raw = result.get("projects")
        projects = projects_raw if isinstance(projects_raw, list) else []
        return CLIResponse(
            ok=True,
            command="projects",
            exit_code=CLIExitCode.OK,
            data={"projects": projects, "count": len(projects)},
            message=f"Found {len(projects)} allowed project(s)",
        )
    except Exception as exc:
        from .errors import ConradDomainError
        if isinstance(exc, ConradDomainError):
            code = map_error_family_to_exit_code(exc.family)
            return CLIResponse(
                ok=False,
                command="projects",
                exit_code=code,
                error=exc.envelope.to_dict(),
                message=exc.envelope.message,
            )
        return CLIResponse(
            ok=False,
            command="projects",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={
                "code": "PROJECT_REGISTRY_ERROR",
                "family": ErrorFamily.VALIDATION_FAILED.value,
                "message": str(exc),
                "remediation": "Ensure projects.json is valid and contains enabled project configurations.",
            },
            message=f"Failed to load projects: {exc}",
        )


def _handle_init(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'init' command."""
    config_path = Path(args.config).resolve()
    state_dir = Path(args.state_dir).resolve()
    rollback_manifest = getattr(args, "rollback_manifest", None)

    from .bootstrap import (
        BootstrapConflictError,
        init_conrad_installation,
        rollback_conrad_config,
    )

    if rollback_manifest:
        try:
            report = rollback_conrad_config(rollback_manifest)
            return CLIResponse(
                ok=True,
                command="init",
                exit_code=CLIExitCode.OK,
                data={
                    "config_path": report.config_path,
                    "backup_path": report.backup_path,
                    "restored_projects": list(report.imported_project_ids),
                },
                message=f"Configuration restored from backup manifest {rollback_manifest}",
            )
        except BootstrapConflictError as exc:
            return CLIResponse(
                ok=False,
                command="init",
                exit_code=CLIExitCode.CONFIG_ERROR,
                error={"message": str(exc)},
                message=f"Rollback failed: {exc}",
            )
        except Exception as exc:
            return CLIResponse(
                ok=False,
                command="init",
                exit_code=CLIExitCode.INTERNAL_ERROR,
                error={"message": str(exc)},
                message=f"Rollback error: {exc}",
            )

    import_from = getattr(args, "import_from", None)
    dry_run = getattr(args, "dry_run", False)
    allow_existing = getattr(args, "allow_existing_with_backup", False)

    try:
        report = init_conrad_installation(
            config_path=config_path,
            state_dir=state_dir,
            import_from=import_from,
            dry_run=dry_run,
            allow_existing_with_backup=allow_existing,
        )
        if not report.ok:
            conflicts_data = [
                {"project_id": c.project_id, "field": c.field, "reason": c.reason, "remediation": c.remediation}
                for c in report.conflicts
            ]
            return CLIResponse(
                ok=False,
                command="init",
                exit_code=CLIExitCode.CONFIG_ERROR,
                data={"config_path": report.config_path, "conflicts": conflicts_data},
                error={"code": "INIT_CONFLICT", "conflicts": conflicts_data},
                message=f"Initialization encountered {len(report.conflicts)} conflict(s)",
            )
        return CLIResponse(
            ok=True,
            command="init",
            exit_code=CLIExitCode.OK,
            data={
                "config_path": report.config_path,
                "state_dir": report.state_dir,
                "dry_run": report.dry_run,
                "backup_path": report.backup_path,
                "backup_manifest_path": report.backup_manifest_path,
                "imported_project_ids": list(report.imported_project_ids),
                "warnings": list(report.warnings),
            },
            message=f"Conrad installation initialized at {report.config_path}",
        )
    except BootstrapConflictError as exc:
        conflicts_data = [
            {"project_id": c.project_id, "field": c.field, "reason": c.reason, "remediation": c.remediation}
            for c in exc.conflicts
        ]
        return CLIResponse(
            ok=False,
            command="init",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "INIT_CONFLICT", "message": str(exc), "conflicts": conflicts_data},
            message=str(exc),
        )
    except Exception as exc:
        return CLIResponse(
            ok=False,
            command="init",
            exit_code=CLIExitCode.INTERNAL_ERROR,
            error={"message": str(exc)},
            message=f"Initialization failed: {exc}",
        )


def _handle_project_add(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'project add' command."""
    config_path = Path(args.config).resolve()
    project_id = args.project_id
    repo_root = Path(args.repo_root).resolve()
    profile = getattr(args, "profile", "readonly")
    trust_mode = getattr(args, "trust_mode", "untrusted")
    branch_prefix = getattr(args, "branch_prefix", "conrad/")
    dry_run = getattr(args, "dry_run", False)

    raw_roots = getattr(args, "write_roots", None)
    if raw_roots:
        roots_list: list[str] = []
        for r in raw_roots:
            for part in str(r).split(","):
                part_clean = part.strip()
                if part_clean:
                    roots_list.append(part_clean)
        write_roots: tuple[str, ...] = tuple(roots_list)
    else:
        write_roots = ("docs",)

    raw_cmds = getattr(args, "verify_cmds", None)
    v_cmds: list[dict[str, Any]] = []
    if raw_cmds:
        for idx, cmd_str in enumerate(raw_cmds):
            if ":" in str(cmd_str):
                cid, carg = str(cmd_str).split(":", 1)
                argv = carg.strip().split()
                v_cmds.append({"id": cid.strip(), "argv": argv, "kind": "unit"})
            else:
                argv = str(cmd_str).strip().split()
                v_cmds.append({"id": f"check-{idx+1}", "argv": argv, "kind": "unit"})

    from .project_registration import register_project

    try:
        result = register_project(
            config_path=config_path,
            project_id=project_id,
            repo_root=repo_root,
            profile=profile,
            trust_mode=trust_mode,
            markdown_write_roots=write_roots,
            verification_commands=tuple(v_cmds),
            branch_prefix=branch_prefix,
            dry_run=dry_run,
        )
        return CLIResponse(
            ok=True,
            command="project add",
            exit_code=CLIExitCode.OK,
            data=result,
            message=f"Project '{project_id}' registered successfully",
        )
    except ValueError as exc:
        return CLIResponse(
            ok=False,
            command="project add",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "REGISTRATION_FAILED", "message": str(exc)},
            message=f"Project registration failed: {exc}",
        )
    except Exception as exc:
        return CLIResponse(
            ok=False,
            command="project add",
            exit_code=CLIExitCode.INTERNAL_ERROR,
            error={"message": str(exc)},
            message=f"Unexpected error registering project: {exc}",
        )


def _handle_project_show(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'project show' command."""
    config_path = Path(args.config).resolve()
    project_id = args.project_id

    from .project_registration import show_project

    try:
        result = show_project(config_path, project_id)
        return CLIResponse(
            ok=True,
            command="project show",
            exit_code=CLIExitCode.OK,
            data=result,
            message=f"Project '{project_id}' found",
        )
    except ValueError as exc:
        return CLIResponse(
            ok=False,
            command="project show",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "PROJECT_NOT_FOUND", "message": str(exc)},
            message=str(exc),
        )
    except Exception as exc:
        return CLIResponse(
            ok=False,
            command="project show",
            exit_code=CLIExitCode.INTERNAL_ERROR,
            error={"message": str(exc)},
            message=f"Error retrieving project: {exc}",
        )


def _handle_project_validate(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'project validate' command."""
    config_path = Path(args.config).resolve()

    from .project_registration import validate_projects_config

    report = validate_projects_config(config_path)
    if report.get("valid", False):
        return CLIResponse(
            ok=True,
            command="project validate",
            exit_code=CLIExitCode.OK,
            data=report,
            message=f"Configuration at {config_path} is valid ({len(report.get('projects', []))} project(s))",
        )
    errors = report.get("errors", [])
    return CLIResponse(
        ok=False,
        command="project validate",
        exit_code=CLIExitCode.CONFIG_ERROR,
        data=report,
        error={"code": "VALIDATION_FAILED", "errors": errors},
        message=f"Projects configuration validation failed with {len(errors)} error(s)",
    )


def _handle_project(args: argparse.Namespace) -> CLIResponse:
    """Router for 'project' subcommands."""
    sub = getattr(args, "project_subcommand", None)
    if sub == "add":
        return _handle_project_add(args)
    if sub == "show":
        return _handle_project_show(args)
    if sub == "validate":
        return _handle_project_validate(args)
    return CLIResponse(
        ok=False,
        command="project",
        exit_code=CLIExitCode.USAGE_ERROR,
        message="Missing project subcommand: choose add, show, or validate",
    )


def _handle_profile_list(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'profile list' command."""
    from .profiles import list_capability_profiles

    profiles = list_capability_profiles()
    data = [
        {
            "name": p.name.value,
            "version": p.version,
            "summary": p.summary,
            "capabilities": sorted(c.value for c in p.capabilities),
            "default_containment_mode": p.default_containment_mode,
            "commit_requires_review": p.commit_requires_review,
            "commit_requires_human_approval": p.commit_requires_human_approval,
            "allows_publication": p.allows_publication,
        }
        for p in profiles
    ]
    return CLIResponse(
        ok=True,
        command="profile list",
        exit_code=CLIExitCode.OK,
        data={"profiles": data, "count": len(data)},
        message=f"Catalog loaded with {len(data)} capability profile(s)",
    )


def _handle_profile_explain(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'profile explain' command."""
    profile_name = args.profile
    trust_mode = getattr(args, "trust_mode", "untrusted")

    from .profiles import explain_capability_profile

    try:
        result = explain_capability_profile(profile_name, trust_mode=trust_mode)
        return CLIResponse(
            ok=True,
            command="profile explain",
            exit_code=CLIExitCode.OK,
            data=result,
            message=f"Capability profile '{profile_name}' explanation ({trust_mode})",
        )
    except ValueError as exc:
        return CLIResponse(
            ok=False,
            command="profile explain",
            exit_code=CLIExitCode.CONFIG_ERROR,
            error={"code": "INVALID_PROFILE", "message": str(exc)},
            message=str(exc),
        )


def _handle_profile(args: argparse.Namespace) -> CLIResponse:
    """Router for 'profile' subcommands."""
    sub = getattr(args, "profile_subcommand", None)
    if sub == "explain":
        return _handle_profile_explain(args)
    return _handle_profile_list(args)


def _handle_doctor(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'doctor' command."""
    config_path = Path(args.config).resolve() if getattr(args, "config", None) else None
    state_dir = Path(args.state_dir).resolve() if getattr(args, "state_dir", None) else None

    raw_transport = getattr(args, "transport", "stdio")
    try:
        transport_mode = TransportMode(raw_transport)
    except ValueError:
        transport_mode = TransportMode.STDIO

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8000)
    profile = getattr(args, "profile", None)
    executor = getattr(args, "executor", None)

    from .doctor import run_conrad_doctor

    report = run_conrad_doctor(
        config_path=config_path,
        state_dir=state_dir,
        transport_mode=transport_mode,
        host=host,
        port=port,
        profile=profile,
        executor_kind=executor,
    )

    exit_code = CLIExitCode.OK if report.overall_healthy else CLIExitCode.DOCTOR_FAILED
    return CLIResponse(
        ok=report.overall_healthy,
        command="doctor",
        exit_code=exit_code,
        data=report.to_dict(),
        error={"summary": dict(report.summary)} if not report.overall_healthy else None,
        message=f"Conrad doctor diagnostics {report.status}: overall_healthy={report.overall_healthy}",
    )


def _handle_onboard_check(args: argparse.Namespace) -> CLIResponse:
    """Handler for 'onboard-check' command."""
    workspace_arg = getattr(args, "workspace", None)
    keep_artifacts = getattr(args, "keep_artifacts", False)

    from .onboarding import run_onboarding_acceptance

    if workspace_arg:
        ws_path = Path(workspace_arg).resolve()
        report = run_onboarding_acceptance(workspace_dir=ws_path, keep_artifacts=keep_artifacts)
    else:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            ws_path = Path(td).resolve()
            report = run_onboarding_acceptance(workspace_dir=ws_path, keep_artifacts=True)

    exit_code = CLIExitCode.OK if report.passed else CLIExitCode.VERIFICATION_ERROR
    return CLIResponse(
        ok=report.passed,
        command="onboard-check",
        exit_code=exit_code,
        data=report.to_dict(),
        error={"blockers": list(report.blockers)} if not report.passed else None,
        message=(
            f"Onboarding acceptance {'passed' if report.passed else 'failed'} "
            f"in {report.total_duration_ms}ms (target < 5min: {report.within_five_minute_target})"
        ),
    )


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    extra_handlers: Mapping[str, Callable[..., CLIResponse]] | None = None,
) -> int:
    """Execute Conrad CLI commands, rendering output and returning integer exit code."""
    out_stream = stdout or sys.stdout
    err_stream = stderr or sys.stderr

    raw_args = list(sys.argv[1:] if argv is None else argv)

    # Check for help flag early to ensure clean 0 exit
    if "-h" in raw_args or "--help" in raw_args:
        parser = build_cli_parser()
        parser.print_help(out_stream)
        return int(CLIExitCode.OK)

    json_flag = "--json" in raw_args

    parser = build_cli_parser()
    try:
        parsed_args = parser.parse_args(raw_args)
    except SystemExit as exc:
        return int(CLIExitCode.USAGE_ERROR)
    except Exception as exc:
        err_resp = CLIResponse(
            ok=False,
            command="cli",
            exit_code=CLIExitCode.USAGE_ERROR,
            error={"message": str(exc)},
            message=f"Command line error: {exc}",
        )
        rendered = render_cli_response(err_resp, json_output=json_flag)
        err_stream.write(rendered + "\n")
        return int(CLIExitCode.USAGE_ERROR)

    is_json = json_flag or bool(getattr(parsed_args, "json", False))

    # Handle global --version flag
    if getattr(parsed_args, "show_version", False):
        resp = _handle_version(parsed_args)
        rendered = render_cli_response(resp, json_output=is_json)
        out_stream.write(rendered + "\n")
        return int(resp.exit_code)

    subcommand = parsed_args.subcommand
    if not subcommand:
        parser.print_help(err_stream)
        return int(CLIExitCode.USAGE_ERROR)

    # Check extra handlers first for extensibility
    if extra_handlers and subcommand in extra_handlers:
        handler = extra_handlers[subcommand]
        resp = handler(parsed_args)
    elif subcommand == "version":
        resp = _handle_version(parsed_args)
    elif subcommand == "start":
        resp = _handle_start(parsed_args)
    elif subcommand == "status":
        resp = _handle_status(parsed_args)
    elif subcommand == "stop":
        resp = _handle_stop(parsed_args)
    elif subcommand == "projects":
        resp = _handle_projects(parsed_args)
    elif subcommand == "init":
        resp = _handle_init(parsed_args)
    elif subcommand == "project":
        resp = _handle_project(parsed_args)
    elif subcommand == "profile":
        resp = _handle_profile(parsed_args)
    elif subcommand == "doctor":
        resp = _handle_doctor(parsed_args)
    elif subcommand == "onboard-check":
        resp = _handle_onboard_check(parsed_args)
    else:
        resp = CLIResponse(
            ok=False,
            command=subcommand,
            exit_code=CLIExitCode.USAGE_ERROR,
            message=f"Unknown or unsupported subcommand: {subcommand}",
        )

    target_stream = out_stream if resp.ok else err_stream
    rendered = render_cli_response(resp, json_output=is_json)
    target_stream.write(rendered + "\n")
    return int(resp.exit_code)


def main() -> None:
    """CLI console entry point."""
    code = run_cli(sys.argv[1:])
    sys.exit(code)


if __name__ == "__main__":
    main()
