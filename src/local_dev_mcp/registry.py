from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import store
from .models import (
    ProjectCapability,
    ProjectPolicy,
    ProjectRegistryDocument,
    _validate_apex_authority_ref,
)


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
    def __init__(
        self,
        projects: dict[str, ProjectPolicy],
        state_db: Any | None = None,
    ):
        self._projects = projects
        # M-GV-03: the durable registration ledger for external apex grounding.
        # When provided, the registry records each grounded project's
        # ``apex_authority_ref`` at load and rejects a ref that changed without
        # an explicit re-confirmation.
        self._state_db = state_db

    @classmethod
    def load(
        cls,
        config_path: Path,
        *,
        require_apex_grounding: bool = False,
        state_db: Any | None = None,
    ) -> ProjectRegistry:
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
            # M-GV-03: when the global REQUIRE_APEX_GROUNDING flag is set, every
            # enabled policy must declare its external apex authority reference.
            # This is a capability-less, opt-in flag: projects that do not use
            # grounding are untouched when it is off.
            if require_apex_grounding and configured.apex_authority_ref is None:
                raise ProjectRegistryError(
                    f"project {configured.id!r} requires apex_authority_ref when "
                    "REQUIRE_APEX_GROUNDING is set (external grounding for apex "
                    "authority)"
                )
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

        # M-GV-03: enforce the registration invariant durably. A grounded
        # project's ``apex_authority_ref`` must be unchanged since registration;
        # a loaded policy whose ref differs from the ledger is a change without
        # re-confirmation and fails closed. The first sight of a grounded
        # project records (registers) its ref.
        if state_db is not None:
            for project_id, project in projects.items():
                if project.apex_authority_ref is None:
                    continue
                recorded = store.load_registered_apex_authority(state_db, project_id)
                if (
                    recorded is not None
                    and recorded["apex_authority_ref"] != project.apex_authority_ref
                ):
                    raise ProjectRegistryError(
                        f"project {project_id!r} apex_authority_ref changed without "
                        "re-confirmation; call confirm_apex_authority to accept the "
                        "new external grounding ref"
                    )
                if recorded is None:
                    store.upsert_registered_apex_authority(
                        state_db, project_id, project.apex_authority_ref
                    )

        return cls(projects, state_db=state_db)

    def confirm_apex_authority(self, project_id: str, apex_authority_ref: str) -> str:
        """Re-confirm a project's apex authority reference (M-GV-03).

        Updating the external grounding ref for a project is a governance
        change: the registry rejects a new ref until it is explicitly
        re-confirmed here. The new ref is validated with the same rules as the
        policy field, then recorded in the durable registration ledger so the
        next ``load`` accepts it. Requires the project to exist in this
        registry and a durable ledger (``state_db``) to write to.
        """
        if project_id not in self._projects:
            raise ProjectRegistryError(f"unknown project_id: {project_id!r}")
        if self._state_db is None:
            raise ProjectRegistryError(
                "confirm_apex_authority requires the registry to be loaded with "
                "a durable state database"
            )
        validated = _validate_apex_authority_ref(apex_authority_ref)
        if validated is None:
            raise ProjectRegistryError("apex_authority_ref must not be empty")
        store.upsert_registered_apex_authority(
            self._state_db, project_id, validated
        )
        return validated

    def list_public(self) -> list[dict[str, object]]:
        return [
            self._projects[project_id].public_metadata()
            for project_id in sorted(self._projects)
        ]

    def projects(self) -> tuple[ProjectPolicy, ...]:
        """Return enabled policies for internal cross-project safety checks."""
        return tuple(self._projects[project_id] for project_id in sorted(self._projects))

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
