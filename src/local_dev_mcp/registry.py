from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from pydantic import ValidationError

from .models import ProjectCapability, ProjectPolicy, ProjectRegistryDocument


class ProjectRegistryError(RuntimeError):
    """Raised when project configuration is invalid or access is denied."""


def _git_root(root: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ProjectRegistryError(f"configured project is not a Git repository: {root}") from exc
    return Path(result.stdout.strip()).resolve(strict=True)


class ProjectRegistry:
    def __init__(self, projects: dict[str, ProjectPolicy]):
        self._projects = projects

    @classmethod
    def load(cls, config_path: Path) -> "ProjectRegistry":
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProjectRegistryError(f"project registry not found: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise ProjectRegistryError(f"project registry is invalid JSON: {exc}") from exc

        try:
            document = ProjectRegistryDocument.model_validate(payload)
        except ValidationError as exc:
            raise ProjectRegistryError(f"project registry validation failed: {exc}") from exc

        projects: dict[str, ProjectPolicy] = {}
        roots: dict[Path, str] = {}
        for configured in document.projects:
            if not configured.enabled:
                continue
            try:
                resolved_root = configured.root.resolve(strict=True)
            except FileNotFoundError as exc:
                raise ProjectRegistryError(
                    f"enabled project root does not exist: {configured.id}"
                ) from exc
            if not resolved_root.is_dir():
                raise ProjectRegistryError(f"project root is not a directory: {configured.id}")
            if resolved_root in roots:
                raise ProjectRegistryError(
                    f"projects {roots[resolved_root]!r} and {configured.id!r} share a root"
                )
            if configured.require_git and _git_root(resolved_root) != resolved_root:
                raise ProjectRegistryError(
                    f"configured root is not the Git top level: {configured.id}"
                )
            project = configured.model_copy(update={"root": resolved_root})
            projects[project.id] = project
            roots[resolved_root] = project.id

        if not projects:
            raise ProjectRegistryError("project registry has no enabled projects")
        return cls(projects)

    def list_public(self) -> list[dict[str, object]]:
        return [
            self._projects[project_id].public_metadata()
            for project_id in sorted(self._projects)
        ]

    def resolve(
        self,
        project_id: str,
        required_capability: ProjectCapability | None = None,
    ) -> ProjectPolicy:
        project = self._projects.get(project_id)
        if project is None:
            raise ProjectRegistryError(
                f"unknown or disabled project_id: {project_id!r}; "
                "call list_allowed_projects"
            )
        if required_capability is not None and required_capability not in project.capabilities:
            raise ProjectRegistryError(
                f"project {project_id!r} does not allow {required_capability.value}"
            )
        return project


class ProjectLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def for_project(self, project_id: str) -> asyncio.Lock:
        async with self._guard:
            return self._locks.setdefault(project_id, asyncio.Lock())

