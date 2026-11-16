"""Read-only repository inspection and profile-based project registration.

Zero FastMCP imports. This module is operator-side tooling: it helps a human
register a Git repository into the trusted ``projects.json`` registry by
expanding a named capability profile, and it validates the resulting
configuration with the exact production loaders (``ProjectPolicy`` /
``ProjectRegistryDocument`` / ``ProjectRegistry``).

Security invariants:

* Git access is strictly read-only (``rev-parse`` / ``branch`` / ``status`` /
  ``symbolic-ref`` / ``remote`` listing) via fixed argument vectors with
  ``subprocess.run`` — never a shell, never a fetch/push/checkout.
* Verification commands supplied at registration are validated and recorded
  in trusted policy, but are NEVER executed here. Execution happens only
  through the governed verification surfaces against an isolated candidate
  worktree, long after registration.
* Registration never modifies the repository; it only reads it and writes
  the operator-owned ``projects.json`` (unless ``dry_run=True``, which
  writes nothing).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    ProjectCapability,
    ProjectPolicy,
    ProjectRegistryDocument,
    VerificationCommand,
)
from .profiles import (
    ProfileName,
    RepositoryTrustMode,
    expand_capability_profile,
)
from .registry import ProjectRegistry, ProjectRegistryError

__all__ = [
    "PROTECTED_PATH_SUGGESTIONS",
    "inspect_git_repository_readonly",
    "register_project",
    "show_project",
    "validate_projects_config",
]

#: Repository locations a registered project should protect from writes by
#: default. Suggested, not forced: the operator may narrow them in the
#: written config, but registration starts from the safe set.
PROTECTED_PATH_SUGGESTIONS: tuple[str, ...] = (
    ".git",
    ".env",
    ".claude",
    ".github/workflows",
)

_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_GIT_TIMEOUT_SECONDS = 10
_MAX_INSPECT_CHANGED_PATHS = 200


def _run_git_readonly(root: Path, *arguments: str) -> str | None:
    """Run one read-only git query with a fixed argv; None on any failure.

    Never uses a shell, never mutates the repository, and is bounded by a
    short timeout so a hung repository cannot stall registration.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None
    return result.stdout.strip()


def _porcelain_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip('"'))
    return paths


def inspect_git_repository_readonly(repo_root: Path) -> dict[str, Any]:
    """Inspect a repository read-only and report what registration needs.

    Never mutates the repository and never raises on non-repositories: a
    path that is missing, not a directory, or not a Git repository is
    reported structurally (``is_git_repository`` False with a ``reason``)
    so an operator can diagnose before registering.
    """
    root = Path(repo_root).expanduser()
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    report: dict[str, Any] = {"repo_root": str(resolved), "is_git_repository": False}
    if not resolved.exists() or not resolved.is_dir():
        report["reason"] = "path does not exist or is not a directory"
        return report
    toplevel = _run_git_readonly(resolved, "rev-parse", "--show-toplevel")
    if toplevel is None:
        report["reason"] = (
            "git rev-parse --show-toplevel failed; this path is not a Git repository"
        )
        return report
    toplevel_path = Path(toplevel).resolve()
    report["is_git_repository"] = True
    report["git_toplevel"] = str(toplevel_path)
    report["root_is_toplevel"] = toplevel_path == resolved

    branch = _run_git_readonly(resolved, "branch", "--show-current")
    head = _run_git_readonly(resolved, "rev-parse", "HEAD")
    status = _run_git_readonly(resolved, "status", "--porcelain") or ""
    changed_paths = _porcelain_paths(status)
    remotes = (_run_git_readonly(resolved, "remote") or "").split()

    report["branch"] = branch or None
    report["head"] = head
    report["clean"] = not changed_paths
    report["changed_count"] = len(changed_paths)
    report["changed_paths"] = sorted(changed_paths)[:_MAX_INSPECT_CHANGED_PATHS]
    report["changed_paths_truncated"] = (
        len(changed_paths) > _MAX_INSPECT_CHANGED_PATHS
    )
    report["default_branch"] = _run_git_readonly(
        resolved, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    )
    report["has_origin_remote"] = "origin" in remotes
    report["protected_paths_present"] = sorted(
        suggestion
        for suggestion in PROTECTED_PATH_SUGGESTIONS
        if (resolved / suggestion).exists()
    )
    return report


def _load_config_entries(config_path: Path) -> list[dict[str, Any]]:
    """Load existing registry entries, or return [] when the file is absent.

    Performs only structural checks (valid JSON object, version 1,
    ``projects`` list of objects) so an operator can still register into a
    config that contains a broken *other* project; the full production
    validation is available via ``validate_projects_config``.
    """
    if not config_path.exists():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    version = payload.get("version", 1)
    if version != 1:
        raise ValueError(f"unsupported registry version in {config_path}: {version!r}")
    projects = payload.get("projects", [])
    if not isinstance(projects, list) or any(
        not isinstance(item, dict) for item in projects
    ):
        raise ValueError(f"'projects' in {config_path} must be a list of objects")
    return [dict(item) for item in projects]


def _normalize_verification_commands(
    commands: Sequence[VerificationCommand | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and normalize verification commands WITHOUT executing them.

    Each entry is validated by the production ``VerificationCommand`` model
    (fixed argv, no shell, unique IDs enforced later by ``ProjectPolicy``)
    and normalized to a JSON-safe dict for the registry file.
    """
    normalized: list[dict[str, Any]] = []
    for item in commands:
        if isinstance(item, VerificationCommand):
            model = item
        elif isinstance(item, Mapping):
            try:
                model = VerificationCommand.model_validate(dict(item))
            except ValidationError as exc:
                raise ValueError(
                    "verification command is invalid: " + str(exc).replace("\n", " ")
                ) from exc
        else:
            raise ValueError(
                "verification_commands entries must be VerificationCommand "
                f"models or mappings, not {type(item).__name__}"
            )
        normalized.append(model.model_dump(mode="json"))
    return normalized


def register_project(
    *,
    config_path: Path,
    project_id: str,
    repo_root: Path,
    profile: ProfileName | str = "readonly",
    trust_mode: RepositoryTrustMode | str = "untrusted",
    markdown_write_roots: Sequence[str] = ("docs",),
    verification_commands: Sequence[Mapping[str, Any]] = (),
    branch_prefix: str = "conrad/",
    containment_available: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register one Git repository under a named capability profile.

    Validates everything the production registry will enforce (repository
    existence, Git top-level root, unique project id and root, full
    ``ProjectPolicy`` dependency closure) and — unless ``dry_run=True`` —
    appends the project entry to ``config_path``. Never executes the
    verification commands; they are recorded in trusted policy only.
    """
    config_path = Path(config_path)
    if not _PROJECT_ID_PATTERN.fullmatch(str(project_id)):
        raise ValueError(
            f"project_id {project_id!r} must match [a-z][a-z0-9-]{{1,63}} "
            "(lowercase letters, digits, hyphens; 2-64 characters)"
        )

    expanded = expand_capability_profile(
        profile,
        trust_mode=trust_mode,
        branch_prefix=branch_prefix,
        containment_available=containment_available,
    )

    root = Path(repo_root).expanduser()
    try:
        resolved_root = root.resolve()
    except OSError as exc:
        raise ValueError(f"repository root could not be resolved: {root}") from exc
    if not resolved_root.exists():
        raise ValueError(f"repository root does not exist: {resolved_root}")
    if not resolved_root.is_dir():
        raise ValueError(f"repository root is not a directory: {resolved_root}")

    inspection = inspect_git_repository_readonly(resolved_root)
    if not inspection["is_git_repository"]:
        raise ValueError(
            f"repository root is not a Git repository: {resolved_root} "
            f"({inspection.get('reason')})"
        )
    if not inspection["root_is_toplevel"]:
        raise ValueError(
            f"repository root is not the Git top level: {resolved_root} "
            f"(top level is {inspection['git_toplevel']}); register the "
            "repository root instead of a subdirectory"
        )

    entries = _load_config_entries(config_path)
    for existing in entries:
        if existing.get("id") == project_id:
            raise ValueError(
                f"project_id {project_id!r} is already registered in {config_path}"
            )
        existing_root = str(existing.get("root", ""))
        try:
            existing_resolved = (
                Path(existing_root).expanduser().resolve() if existing_root else None
            )
        except OSError:
            existing_resolved = None
        if existing_resolved == resolved_root:
            raise ValueError(
                f"repository root {resolved_root} is already registered as "
                f"project {existing.get('id')!r}"
            )

    capabilities = frozenset(
        ProjectCapability(value) for value in expanded.capabilities
    )
    normalized_commands = _normalize_verification_commands(verification_commands)

    entry: dict[str, Any] = {
        "id": project_id,
        "display_name": project_id,
        "description": (
            f"Registered via capability profile "
            f"'{expanded.profile_name.value}' (trust: {expanded.trust_mode.value})"
        ),
        "root": str(resolved_root),
        "enabled": True,
        "require_git": True,
        "capabilities": list(expanded.capabilities),
        "markdown_write_roots": [str(item) for item in markdown_write_roots],
        "protected_paths": list(PROTECTED_PATH_SUGGESTIONS),
        "requires_review": expanded.commit_requires_review,
        "commit_roots": (
            ["."] if ProjectCapability.VERIFICATION_COMMIT in capabilities else []
        ),
        "verification_commands": normalized_commands,
    }

    try:
        ProjectPolicy.model_validate(entry)
    except ValidationError as exc:
        raise ValueError(
            f"registered project policy for {project_id!r} is invalid: "
            + str(exc).replace("\n", " ")
        ) from exc

    warnings: list[str] = []
    if not inspection["clean"]:
        warnings.append(
            "repository working tree is dirty; registration does not capture, "
            "commit, or modify these changes"
        )
    if not inspection["has_origin_remote"]:
        warnings.append(
            "no 'origin' Git remote detected; publication and canonical sync "
            "flows will be unavailable until a remote is configured"
        )

    if not dry_run:
        document = {"version": 1, "projects": entries + [entry]}
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    return {
        "registered": not dry_run,
        "dry_run": bool(dry_run),
        "config_path": str(config_path),
        "project_id": project_id,
        "project_entry": entry,
        "profile": expanded.profile_name.value,
        "profile_version": expanded.profile_version,
        "trust_mode": expanded.trust_mode.value,
        "capabilities": list(expanded.capabilities),
        "containment_mode": expanded.containment_mode,
        "authority_summary": expanded.authority_summary,
        "policy_digest": expanded.policy_digest,
        "repo_root": str(resolved_root),
        "repo_is_clean": inspection["clean"],
        "verification_commands_recorded": len(normalized_commands),
        # Registration is read-only against the repository: no verification
        # command is ever executed here.
        "verification_commands_executed": 0,
        "inspection": inspection,
        "warnings": warnings,
    }


def show_project(config_path: Path, project_id: str) -> dict[str, Any]:
    """Show one registered project entry and whether it validates.

    Raises ``ValueError`` when the config is unusable or the project is not
    registered; an entry that fails policy validation is reported
    (``policy_valid`` False with the validation error) rather than raised,
    so the operator can diagnose a broken entry in place.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise ValueError(f"project registry not found: {config_path}")
    entries = _load_config_entries(config_path)
    for entry in entries:
        if entry.get("id") != project_id:
            continue
        policy_valid = True
        validation_error: str | None = None
        try:
            ProjectPolicy.model_validate(entry)
        except ValidationError as exc:
            policy_valid = False
            validation_error = str(exc).replace("\n", " ")
        return {
            "config_path": str(config_path),
            "project_id": project_id,
            "project": entry,
            "policy_valid": policy_valid,
            "validation_error": validation_error,
        }
    raise ValueError(
        f"project {project_id!r} is not registered in {config_path}"
    )


def validate_projects_config(config_path: Path) -> dict[str, Any]:
    """Validate a projects registry with the exact production loaders.

    Returns a structured report instead of raising: schema-level failures
    are pinpointed per project id, and a structurally valid document is put
    through ``ProjectRegistry.load`` so every runtime gate (root existence,
    Git top level, unique roots, at least one enabled project) is checked
    with the same code the bridge boots with.
    """
    config_path = Path(config_path)
    report: dict[str, Any] = {
        "config_path": str(config_path),
        "valid": False,
        "errors": [],
        "warnings": [],
        "projects": [],
    }

    if not config_path.exists():
        report["errors"].append(f"project registry not found: {config_path}")
        return report
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report["errors"].append(f"project registry is invalid JSON: {exc}")
        return report

    raw_projects = payload.get("projects", []) if isinstance(payload, dict) else []
    if not isinstance(raw_projects, list):
        raw_projects = []

    try:
        document = ProjectRegistryDocument.model_validate(payload)
    except ValidationError as exc:
        report["errors"].append(f"project registry validation failed: {exc}")
        # Pinpoint per-project failures so the operator sees which entry
        # (and which id) is broken, not just the document-level error.
        for raw in raw_projects:
            if not isinstance(raw, dict):
                continue
            project_id = str(raw.get("id", "<missing id>"))
            try:
                ProjectPolicy.model_validate(raw)
                per_project_error = None
            except ValidationError as project_exc:
                per_project_error = str(project_exc).replace("\n", " ")
            report["projects"].append(
                {
                    "id": project_id,
                    "enabled": bool(raw.get("enabled", True)),
                    "valid": per_project_error is None,
                    "errors": [per_project_error] if per_project_error else [],
                }
            )
        return report

    report["version"] = document.version
    report["project_count"] = len(document.projects)
    report["enabled_project_count"] = sum(
        1 for project in document.projects if project.enabled
    )
    report["project_ids"] = [project.id for project in document.projects]
    report["projects"] = [
        {
            "id": project.id,
            "enabled": project.enabled,
            "valid": True,
            "errors": [],
        }
        for project in document.projects
    ]

    try:
        registry = ProjectRegistry.load(config_path)
        registry.close()
    except ProjectRegistryError as exc:
        report["errors"].append(str(exc))

    report["valid"] = not report["errors"]
    return report
