from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Effort = Literal["high", "ultracode"]


class ProjectCapability(StrEnum):
    REPOSITORY_READ = "repository_read"
    MARKDOWN_WRITE = "markdown_write"
    CLAUDE_EDIT = "claude_edit"


def _policy_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("policy paths must be non-empty relative paths without '..'")
    return path.as_posix()


class ProjectPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    root: Path
    enabled: bool = True
    require_git: bool = True
    capabilities: frozenset[ProjectCapability]
    markdown_write_roots: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = (".git", ".env")
    default_effort: Effort = "high"
    allow_ultracode: bool = True
    self_managed: bool = False
    claude_model: str | None = None
    claude_max_turns: int | None = Field(default=None, ge=1, le=200)

    @field_validator("root", mode="before")
    @classmethod
    def validate_root(cls, value: object) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("project root must be absolute")
        return path

    @field_validator("markdown_write_roots", "protected_paths", mode="before")
    @classmethod
    def validate_policy_paths(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        return tuple(_policy_path(str(item)) for item in value)

    @field_validator("claude_model")
    @classmethod
    def validate_optional_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("claude_model must not be empty")
        return stripped

    @model_validator(mode="after")
    def validate_policy(self) -> "ProjectPolicy":
        if self.default_effort == "ultracode" and not self.allow_ultracode:
            raise ValueError("default_effort cannot be ultracode when it is disabled")
        if self.self_managed and ProjectCapability.MARKDOWN_WRITE in self.capabilities:
            raise ValueError(
                "self-managed projects cannot use direct markdown_write against the "
                "production checkout"
            )
        if (
            ProjectCapability.MARKDOWN_WRITE in self.capabilities
            and not self.markdown_write_roots
        ):
            raise ValueError("markdown_write requires at least one approved write root")
        return self

    def public_metadata(self) -> dict[str, object]:
        return {
            "project_id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "default_effort": self.default_effort,
            "allow_ultracode": self.allow_ultracode,
            "self_managed": self.self_managed,
        }


class ProjectRegistryDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    projects: tuple[ProjectPolicy, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ProjectRegistryDocument":
        ids = [project.id for project in self.projects]
        if len(ids) != len(set(ids)):
            raise ValueError("project IDs must be unique")
        return self
