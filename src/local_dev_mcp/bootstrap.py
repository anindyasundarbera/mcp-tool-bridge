"""Safe Bootstrap & BridgeLab Configuration Import.

Phase 3 Operator Experience (M3.2 / A3.2.1):
Safe, fail-closed bootstrap initialization and configuration import for
Conrad / BridgeLab environments. Zero fastmcp imports.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from .models import ProjectCapability, ProjectPolicy, ProjectRegistryDocument
from .registry import ProjectRegistry, ProjectRegistryError

CONFIG_SCHEMA_VERSION: str = "1.0"

# Register ProjectRegistry.from_file as an alias to load if not already present
if not hasattr(ProjectRegistry, "from_file"):
    setattr(ProjectRegistry, "from_file", staticmethod(ProjectRegistry.load))  # type: ignore[attr-defined]

_SECRET_KEY_REGEX = re.compile(
    r"(?:^OPENAI_API_KEY$|^ANTHROPIC_API_KEY$|^NGROK_AUTHTOKEN$|^GITHUB_TOKEN$|.*(?:SECRET|TOKEN|API_KEY|PASSWORD).*)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BootstrapConflictItem:
    """A specific conflict, validation error, or safety violation encountered during bootstrap."""

    project_id: str
    field: str
    reason: str
    remediation: str


@dataclass(frozen=True, slots=True)
class BootstrapFileRecord:
    """Record of a file or directory created, backed up, imported, or unchanged during bootstrap."""

    path: str
    mode_octal: str
    sha256: str
    action: str  # "created", "backed_up", "imported", "unchanged", "restored"


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """Comprehensive report returned by bootstrap or rollback operations."""

    ok: bool
    dry_run: bool
    config_schema_version: str
    config_path: str
    state_dir: str
    backup_path: str | None = None
    backup_manifest_path: str | None = None
    files: tuple[BootstrapFileRecord, ...] = ()
    imported_project_ids: tuple[str, ...] = ()
    conflicts: tuple[BootstrapConflictItem, ...] = ()
    diff_summary: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class BootstrapConflictError(RuntimeError):
    """Raised when bootstrap encounters configuration conflicts, unpermitted existing files, or validation errors."""

    def __init__(
        self,
        message: str,
        *,
        conflicts: tuple[BootstrapConflictItem, ...] = (),
        report: BootstrapReport | None = None,
    ) -> None:
        super().__init__(message)
        self.conflicts = conflicts
        self.report = report


def _octal_mode(mode_int: int) -> str:
    """Format an integer file mode as a 4-digit octal string (e.g. '0600')."""
    return oct(mode_int & 0o777)[2:].zfill(4)


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bytes_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def _git_top_level(root: Path) -> Path | None:
    """Return the git top-level directory for root, or None if not a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return Path(result.stdout.strip()).resolve(strict=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def _find_secret_keys(obj: Any, current_path: str, pid: str) -> list[BootstrapConflictItem]:
    """Recursively search for secret-like keys in a configuration dictionary/list."""
    items: list[BootstrapConflictItem] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_str = str(k)
            subpath = f"{current_path}.{key_str}" if current_path else key_str
            if _SECRET_KEY_REGEX.search(key_str):
                items.append(
                    BootstrapConflictItem(
                        project_id=pid,
                        field=subpath,
                        reason=(
                            f"Secret-like key '{key_str}' detected in configuration; "
                            "secrets must remain external in environment or secret managers"
                        ),
                        remediation="Remove secret from project configuration and supply it via environment variables",
                    )
                )
            items.extend(_find_secret_keys(v, subpath, pid))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            subpath = f"{current_path}[{idx}]"
            items.extend(_find_secret_keys(item, subpath, pid))
    return items


def _read_existing_project_ids(config_path: Path) -> list[str]:
    """Safely read existing project IDs from a config file if it exists."""
    if not config_path.exists():
        return []
    try:
        content = config_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("projects"), list):
            result: list[str] = []
            for p in data["projects"]:
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    result.append(p["id"])
            return result
    except Exception:
        pass
    return []


def init_conrad_installation(
    *,
    config_path: Path | str,
    state_dir: Path | str,
    import_from: Path | str | None = None,
    dry_run: bool = False,
    allow_existing_with_backup: bool = False,
) -> BootstrapReport:
    """Initialize a Conrad / BridgeLab installation.

    Generates a public-safe initial configuration or imports an existing
    BridgeLab configuration safely, with no silent overwrite, strict validation,
    atomic activation, and optional backup.
    """
    cfg_path = Path(config_path).expanduser().resolve()
    st_dir = Path(state_dir).expanduser().resolve()
    imp_path = Path(import_from).expanduser().resolve() if import_from is not None else None

    existing_projects = _read_existing_project_ids(cfg_path)
    conflicts: list[BootstrapConflictItem] = []
    warnings: list[str] = []

    # Check for existing config collision
    has_existing = cfg_path.exists()
    if has_existing and not allow_existing_with_backup:
        conflict = BootstrapConflictItem(
            project_id="",
            field="config_path",
            reason=f"Configuration file already exists at {cfg_path}",
            remediation=(
                "Pass allow_existing_with_backup=True to create a backup and replace, "
                "or specify a different config_path"
            ),
        )
        conflicts.append(conflict)
        diff_summary: dict[str, Any] = {
            "added_projects": [],
            "existing_projects": existing_projects,
            "capabilities_by_project": {},
        }
        report = BootstrapReport(
            ok=False,
            dry_run=dry_run,
            config_schema_version=CONFIG_SCHEMA_VERSION,
            config_path=str(cfg_path),
            state_dir=str(st_dir),
            conflicts=tuple(conflicts),
            diff_summary=diff_summary,
            warnings=tuple(warnings),
        )
        if not dry_run:
            raise BootstrapConflictError(
                f"Configuration file already exists at {cfg_path}",
                conflicts=tuple(conflicts),
                report=report,
            )
        return report

    # Prepare project list to write
    valid_policies: list[ProjectPolicy] = []
    candidate_projects_data: list[dict[str, Any]] = []

    if imp_path is not None:
        if not imp_path.exists():
            raise BootstrapConflictError(f"Import source file does not exist: {imp_path}")
        try:
            content = imp_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BootstrapConflictError(f"Cannot read import source {imp_path}: {exc}") from exc
        try:
            raw_data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise BootstrapConflictError(f"Import source {imp_path} is invalid JSON: {exc}") from exc

        if isinstance(raw_data, list):
            raw_projects = raw_data
        elif isinstance(raw_data, dict) and "projects" in raw_data and isinstance(raw_data["projects"], list):
            raw_projects = raw_data["projects"]
        else:
            raise BootstrapConflictError(
                f"Invalid format in import source {imp_path}: expected JSON object with 'projects' list"
            )

        seen_pids: set[str] = set()
        seen_roots: dict[Path, str] = {}

        for raw_proj in raw_projects:
            if not isinstance(raw_proj, dict):
                conflicts.append(
                    BootstrapConflictItem(
                        project_id="",
                        field="projects",
                        reason="Project entry is not a JSON object",
                        remediation="Ensure all items in 'projects' are dictionary objects",
                    )
                )
                continue

            pid = raw_proj.get("id")
            if not isinstance(pid, str) or not pid:
                conflicts.append(
                    BootstrapConflictItem(
                        project_id="",
                        field="id",
                        reason="Project 'id' is required and must be a non-empty string",
                        remediation="Assign a valid unique string identifier to the project",
                    )
                )
                pid_str = "<unknown>"
            else:
                pid_str = pid
                if pid_str in seen_pids:
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field="id",
                            reason=f"Duplicate project_id '{pid_str}' found in configuration",
                            remediation="Ensure each project in projects.json has a unique id",
                        )
                    )
                seen_pids.add(pid_str)

            # Check secrets in this project entry
            secret_conflicts = _find_secret_keys(raw_proj, "", pid_str)
            conflicts.extend(secret_conflicts)

            # Check project root path
            raw_root = raw_proj.get("root")
            root_ok = False
            resolved_root: Path | None = None
            if not raw_root:
                conflicts.append(
                    BootstrapConflictItem(
                        project_id=pid_str,
                        field="root",
                        reason="Project 'root' is missing or empty",
                        remediation="Specify an absolute directory path for the project root",
                    )
                )
            else:
                root_str = str(raw_root)
                root_path = Path(root_str)
                if not root_path.is_absolute():
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field="root",
                            reason=f"Project root '{root_str}' is a relative path; must be absolute",
                            remediation="Specify an absolute path for project root",
                        )
                    )
                elif not root_path.exists():
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field="root",
                            reason=f"Project root does not exist: {root_path}",
                            remediation="Ensure project root directory exists before importing",
                        )
                    )
                elif not root_path.is_dir():
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field="root",
                            reason=f"Project root is not a directory: {root_path}",
                            remediation="Project root must be a directory",
                        )
                    )
                else:
                    try:
                        resolved_root = root_path.resolve(strict=True)
                        if resolved_root in seen_roots:
                            conflicts.append(
                                BootstrapConflictItem(
                                    project_id=pid_str,
                                    field="root",
                                    reason=(
                                        f"Projects '{seen_roots[resolved_root]}' and '{pid_str}' "
                                        f"share the same root: {resolved_root}"
                                    ),
                                    remediation="Each project must have a distinct root directory",
                                )
                            )
                        else:
                            seen_roots[resolved_root] = pid_str
                            root_ok = True
                    except Exception as exc:
                        conflicts.append(
                            BootstrapConflictItem(
                                project_id=pid_str,
                                field="root",
                                reason=f"Failed to resolve root path: {exc}",
                                remediation="Ensure project root path is accessible and valid",
                            )
                        )

            # Check Git repository requirement
            require_git = raw_proj.get("require_git", True)
            if root_ok and resolved_root is not None and require_git:
                top_lvl = _git_top_level(resolved_root)
                if top_lvl is None or top_lvl != resolved_root:
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field="root",
                            reason=f"Configured root is not the Git top level: {resolved_root}",
                            remediation=(
                                "Ensure root is the repository root containing .git, "
                                "or set require_git to false"
                            ),
                        )
                    )

            # Validate against ProjectPolicy schema and capability closure
            try:
                policy = ProjectPolicy.model_validate(raw_proj)
                valid_policies.append(policy)
                candidate_projects_data.append(policy.model_dump(mode="json"))
            except ValidationError as exc:
                for err in exc.errors():
                    field_name = ".".join(str(loc) for loc in err.get("loc", ()))
                    conflicts.append(
                        BootstrapConflictItem(
                            project_id=pid_str,
                            field=field_name,
                            reason=err.get("msg", str(err)),
                            remediation="Update project configuration to satisfy policy and capability requirements",
                        )
                    )
            except Exception as exc:
                conflicts.append(
                    BootstrapConflictItem(
                        project_id=pid_str,
                        field="policy",
                        reason=str(exc),
                        remediation="Update project configuration to conform to ProjectPolicy requirements",
                    )
                )

    # Compute diff summary
    added_project_ids = [p.id for p in valid_policies]
    capabilities_by_project = {
        p.id: sorted(c.value if hasattr(c, "value") else str(c) for c in p.capabilities)
        for p in valid_policies
    }
    diff_summary = {
        "added_projects": added_project_ids,
        "existing_projects": existing_projects,
        "capabilities_by_project": capabilities_by_project,
    }

    # If conflicts were found, fail closed
    if conflicts:
        report = BootstrapReport(
            ok=False,
            dry_run=dry_run,
            config_schema_version=CONFIG_SCHEMA_VERSION,
            config_path=str(cfg_path),
            state_dir=str(st_dir),
            conflicts=tuple(conflicts),
            diff_summary=diff_summary,
            warnings=tuple(warnings),
        )
        if not dry_run:
            raise BootstrapConflictError(
                f"Configuration validation failed with {len(conflicts)} conflict(s)",
                conflicts=tuple(conflicts),
                report=report,
            )
        return report

    # Candidate configuration payload
    candidate_doc = {
        "version": 1,
        "projects": candidate_projects_data,
    }
    candidate_bytes = json.dumps(candidate_doc, indent=2).encode("utf-8") + b"\n"
    candidate_sha = _bytes_sha256(candidate_bytes)

    # In dry_run mode, do not write anything to disk
    if dry_run:
        planned_files: list[BootstrapFileRecord] = []
        backup_plan_path: str | None = None
        manifest_plan_path: str | None = None

        if has_existing and allow_existing_with_backup:
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
            backup_plan_path = str(cfg_path.parent / f"{cfg_path.name}.bak.{now_str}")
            manifest_plan_path = str(cfg_path.parent / f"{cfg_path.name}.backup-manifest.json")
            orig_sha = _file_sha256(cfg_path)
            planned_files.append(
                BootstrapFileRecord(
                    path=backup_plan_path,
                    mode_octal="0600",
                    sha256=orig_sha,
                    action="backed_up",
                )
            )
            planned_files.append(
                BootstrapFileRecord(
                    path=manifest_plan_path,
                    mode_octal="0600",
                    sha256="",
                    action="created",
                )
            )

        planned_files.append(
            BootstrapFileRecord(
                path=str(cfg_path),
                mode_octal="0600",
                sha256=candidate_sha,
                action="imported" if imp_path is not None else "created",
            )
        )

        return BootstrapReport(
            ok=True,
            dry_run=True,
            config_schema_version=CONFIG_SCHEMA_VERSION,
            config_path=str(cfg_path),
            state_dir=str(st_dir),
            backup_path=backup_plan_path,
            backup_manifest_path=manifest_plan_path,
            files=tuple(planned_files),
            imported_project_ids=tuple(added_project_ids),
            conflicts=(),
            diff_summary=diff_summary,
            warnings=tuple(warnings),
        )

    # Live execution: dry_run is False
    try:
        # Create state_dir with mode 0o700
        st_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(st_dir, 0o700)

        # Create config parent directory
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapConflictError(f"Failed to create directory structure: {exc}") from exc

    backup_path_obj: Path | None = None
    manifest_path_obj: Path | None = None
    files_created: list[BootstrapFileRecord] = []

    # If existing and allowed with backup, create backup and manifest
    if has_existing and allow_existing_with_backup:
        try:
            original_bytes = cfg_path.read_bytes()
            original_sha = _bytes_sha256(original_bytes)
            timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
            backup_path_obj = cfg_path.parent / f"{cfg_path.name}.bak.{timestamp_str}"
            manifest_path_obj = cfg_path.parent / f"{cfg_path.name}.backup-manifest.json"

            # Write backup byte-for-byte
            backup_path_obj.write_bytes(original_bytes)
            os.chmod(backup_path_obj, 0o600)
            files_created.append(
                BootstrapFileRecord(
                    path=str(backup_path_obj),
                    mode_octal="0600",
                    sha256=original_sha,
                    action="backed_up",
                )
            )

            # Write manifest
            manifest_data = {
                "original_path": str(cfg_path),
                "backup_path": str(backup_path_obj),
                "original_sha256": original_sha,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "config_schema_version": CONFIG_SCHEMA_VERSION,
            }
            manifest_bytes = json.dumps(manifest_data, indent=2).encode("utf-8") + b"\n"
            manifest_path_obj.write_bytes(manifest_bytes)
            os.chmod(manifest_path_obj, 0o600)
            files_created.append(
                BootstrapFileRecord(
                    path=str(manifest_path_obj),
                    mode_octal="0600",
                    sha256=_bytes_sha256(manifest_bytes),
                    action="created",
                )
            )
        except OSError as exc:
            raise BootstrapConflictError(f"Failed to create backup: {exc}") from exc

    # Write candidate to a temporary file in cfg_path.parent and validate
    temp_fd, temp_path_str = tempfile.mkstemp(
        prefix=f".{cfg_path.name}.tmp-",
        dir=str(cfg_path.parent),
    )
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(temp_fd, "wb") as f:
            f.write(candidate_bytes)
        os.chmod(temp_path, 0o600)

        # Validate candidate file
        if valid_policies:
            # Full validation using ProjectRegistry
            ProjectRegistry.load(temp_path)
        else:
            # Empty template validation
            chk = json.loads(temp_path.read_text(encoding="utf-8"))
            if chk.get("version") != 1 or chk.get("projects") != []:
                raise ValueError("Initial template must have version 1 and empty projects list")

        # Atomically replace target
        os.replace(temp_path, cfg_path)
        os.chmod(cfg_path, 0o600)

        files_created.append(
            BootstrapFileRecord(
                path=str(cfg_path),
                mode_octal="0600",
                sha256=candidate_sha,
                action="imported" if imp_path is not None else "created",
            )
        )
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise BootstrapConflictError(f"Failed to activate configuration: {exc}") from exc

    return BootstrapReport(
        ok=True,
        dry_run=False,
        config_schema_version=CONFIG_SCHEMA_VERSION,
        config_path=str(cfg_path),
        state_dir=str(st_dir),
        backup_path=str(backup_path_obj) if backup_path_obj else None,
        backup_manifest_path=str(manifest_path_obj) if manifest_path_obj else None,
        files=tuple(files_created),
        imported_project_ids=tuple(added_project_ids),
        conflicts=(),
        diff_summary=diff_summary,
        warnings=tuple(warnings),
    )


def rollback_conrad_config(backup_manifest_path: Path | str) -> BootstrapReport:
    """Roll back a configuration to the backup recorded in backup_manifest_path.

    Verifies the backup file exists and matches the recorded original_sha256,
    failing closed if tampered with, missing, or corrupted.
    """
    manifest_path = Path(backup_manifest_path).expanduser().resolve()
    if not manifest_path.exists():
        raise BootstrapConflictError(f"Backup manifest not found: {manifest_path}")

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BootstrapConflictError(f"Failed to read backup manifest {manifest_path}: {exc}") from exc

    if not isinstance(manifest_data, dict):
        raise BootstrapConflictError(f"Backup manifest is not a valid JSON object: {manifest_path}")

    original_path_str = manifest_data.get("original_path")
    backup_path_str = manifest_data.get("backup_path")
    original_sha256 = manifest_data.get("original_sha256")
    schema_version = manifest_data.get("config_schema_version", CONFIG_SCHEMA_VERSION)

    if not original_path_str or not backup_path_str or not original_sha256:
        raise BootstrapConflictError(
            f"Backup manifest {manifest_path} is missing required fields "
            "(original_path, backup_path, original_sha256)"
        )

    original_path = Path(original_path_str).resolve()
    backup_path = Path(backup_path_str).resolve()

    if not backup_path.exists():
        raise BootstrapConflictError(f"Backup file not found: {backup_path}")

    try:
        backup_bytes = backup_path.read_bytes()
    except OSError as exc:
        raise BootstrapConflictError(f"Cannot read backup file {backup_path}: {exc}") from exc

    current_sha256 = _bytes_sha256(backup_bytes)
    if current_sha256 != original_sha256:
        raise BootstrapConflictError(
            f"Backup file at {backup_path} has been tampered with or corrupted: "
            f"expected SHA-256 {original_sha256}, got {current_sha256}"
        )

    # Atomically restore
    original_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path_str = tempfile.mkstemp(
        prefix=f".{original_path.name}.restore-",
        dir=str(original_path.parent),
    )
    temp_path = Path(temp_path_str)
    try:
        with os.fdopen(temp_fd, "wb") as f:
            f.write(backup_bytes)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, original_path)
        os.chmod(original_path, 0o600)
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise BootstrapConflictError(f"Failed to restore backup: {exc}") from exc

    # Parse restored project IDs if possible
    imported_ids: list[str] = []
    try:
        restored_json = json.loads(backup_bytes.decode("utf-8"))
        if isinstance(restored_json, dict) and isinstance(restored_json.get("projects"), list):
            for p in restored_json["projects"]:
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    imported_ids.append(p["id"])
    except Exception:
        pass

    file_record = BootstrapFileRecord(
        path=str(original_path),
        mode_octal="0600",
        sha256=current_sha256,
        action="imported",
    )

    return BootstrapReport(
        ok=True,
        dry_run=False,
        config_schema_version=str(schema_version),
        config_path=str(original_path),
        state_dir=str(original_path.parent),
        backup_path=str(backup_path),
        backup_manifest_path=str(manifest_path),
        files=(file_record,),
        imported_project_ids=tuple(imported_ids),
        conflicts=(),
        diff_summary={"restored_projects": imported_ids},
        warnings=(),
    )


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "BootstrapConflictError",
    "BootstrapConflictItem",
    "BootstrapFileRecord",
    "BootstrapReport",
    "init_conrad_installation",
    "rollback_conrad_config",
]
