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
    VERIFICATION_COMMIT = "verification_commit"
    CANDIDATE_REVIEW = "candidate_review"
    PUSH_DEVELOPMENT_COMMIT = "push_development_commit"
    SYNC_CANONICAL_CHECKOUT = "sync_canonical_checkout"
    EXPORT_EVIDENCE_BUNDLE = "export_evidence_bundle"


def _policy_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("policy paths must be non-empty relative paths without '..'")
    return path.as_posix()


class VerificationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...]
    python_path: tuple[str, ...] = ()

    @field_validator("argv", mode="before")
    @classmethod
    def validate_argv(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, (str, bytes)) or value is None:
            raise ValueError("verification argv must be a list of arguments")
        arguments = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if not 1 <= len(arguments) <= 64:
            raise ValueError("verification argv must contain 1 through 64 arguments")
        for argument in arguments:
            if not argument or len(argument) > 2000 or "\x00" in argument:
                raise ValueError(
                    "verification arguments must be non-empty, NUL-free, and at "
                    "most 2000 characters"
                )
        if "{python}" in arguments[1:]:
            raise ValueError("{python} is allowed only as the verification executable")
        executable = PurePosixPath(arguments[0].replace("\\", "/")).name.lower()
        if executable.removesuffix(".exe") in {
            "sh",
            "bash",
            "dash",
            "zsh",
            "ksh",
            "fish",
            "powershell",
            "pwsh",
            "cmd",
        }:
            raise ValueError("verification commands must not invoke a shell")
        return arguments

    @field_validator("python_path", mode="before")
    @classmethod
    def validate_python_path(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("python_path must be a list of relative directories")
        paths = tuple(_policy_path(str(item)) for item in value)  # type: ignore[arg-type]
        if len(paths) > 16:
            raise ValueError("python_path may contain at most 16 entries")
        return paths


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
    commit_roots: tuple[str, ...] = ()
    verification_commands: tuple[VerificationCommand, ...] = ()
    protected_paths: tuple[str, ...] = (".git", ".env")
    default_effort: Effort = "high"
    allow_ultracode: bool = True
    self_managed: bool = False
    requires_review: bool = False
    allow_bridge_reviewer: bool = False
    claude_model: str | None = None
    claude_max_turns: int | None = Field(default=None, ge=1, le=200)

    @field_validator("root", mode="before")
    @classmethod
    def validate_root(cls, value: object) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("project root must be absolute")
        return path

    @field_validator(
        "markdown_write_roots", "commit_roots", "protected_paths", mode="before"
    )
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
            ProjectCapability.CANDIDATE_REVIEW in self.capabilities
            and ProjectCapability.CLAUDE_EDIT not in self.capabilities
        ):
            raise ValueError("candidate_review requires claude_edit")
        if (
            self.requires_review
            and ProjectCapability.VERIFICATION_COMMIT not in self.capabilities
        ):
            raise ValueError("requires_review requires verification_commit")
        if (
            self.requires_review
            and ProjectCapability.CANDIDATE_REVIEW not in self.capabilities
        ):
            raise ValueError(
                "requires_review requires candidate_review "
                "(otherwise no review can be recorded and the gate deadlocks)"
            )
        if (
            self.allow_bridge_reviewer
            and ProjectCapability.CANDIDATE_REVIEW not in self.capabilities
        ):
            raise ValueError("allow_bridge_reviewer requires candidate_review")
        if (
            ProjectCapability.MARKDOWN_WRITE in self.capabilities
            and not self.markdown_write_roots
        ):
            raise ValueError("markdown_write requires at least one approved write root")
        if ProjectCapability.VERIFICATION_COMMIT in self.capabilities:
            if ProjectCapability.CLAUDE_EDIT not in self.capabilities:
                raise ValueError("verification_commit requires claude_edit")
            if not self.commit_roots:
                raise ValueError(
                    "verification_commit requires at least one approved commit root"
                )
            if not self.verification_commands:
                raise ValueError(
                    "verification_commit requires at least one verification command"
                )
        if (
            ProjectCapability.PUSH_DEVELOPMENT_COMMIT in self.capabilities
            and ProjectCapability.VERIFICATION_COMMIT not in self.capabilities
        ):
            raise ValueError("push_development_commit requires verification_commit")
        if (
            ProjectCapability.SYNC_CANONICAL_CHECKOUT in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError("sync_canonical_checkout requires repository_read")
        if (
            ProjectCapability.EXPORT_EVIDENCE_BUNDLE in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError("export_evidence_bundle requires repository_read")
        command_ids = [command.id for command in self.verification_commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("verification command IDs must be unique per project")
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
