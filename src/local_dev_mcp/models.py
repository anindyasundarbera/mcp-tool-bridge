from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

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
    OPEN_PULL_REQUEST = "open_pull_request"
    # M-AL-01 read-only introspection capabilities (dormant by default). A project
    # opts in by listing one; each requires its prerequisite capability so the
    # authorization boundary never widens beyond what the project already grants.
    JOB_INTROSPECTION = "job_introspection"
    POLICY_INTROSPECTION = "policy_introspection"
    CANONICAL_HISTORY_READ = "canonical_history_read"
    # MCPB-011: governed execution of one named, policy-authorized runtime
    # verification command against the canonical checkout. Host-runtime access
    # (Docker, loopback networking, host services) derives only from trusted
    # configuration; the MCP caller selects only a check id. Evidence-only: no
    # commit, push, merge, tag, or ref authority. Requires repository_read.
    RUNTIME_VERIFICATION = "runtime_verification"
    # MCPB-012: governed capture of an approved set of uncommitted working-tree
    # changes into a retained BridgeLab candidate, without modifying the
    # canonical checkout. Read-only against the source checkout; produces a
    # normal retained candidate eligible for the existing verification, review,
    # commit, and publication gates. The approved include/protected-exclude
    # scope is pinned in trusted policy, not caller-chosen. Requires
    # repository_read (the downstream commit/publish gates carry their own
    # authorization).
    WORKING_TREE_CANDIDATE_CAPTURE = "working_tree_candidate_capture"
    GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION = "governed_disposable_runtime_action_execution"


def _policy_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError("policy paths must be non-empty relative paths without '..'")
    return path.as_posix()


def _validate_apex_authority_ref(value: object) -> str | None:
    """Validate an optional ``ProjectPolicy.apex_authority_ref`` (M-GV-03).

    The external grounding reference for apex authority is either a URL
    (``scheme://target``) or a repo-local pin (``<relative-path>@<git-sha>``,
    a path in the repo plus the commit SHA the authority is grounded at).
    The ref must be non-empty, NUL/control-free, at most 512 characters, and
    must not smuggle an absolute or traversal path in the repo-path form. A
    URL scheme must look like a real scheme and the remainder must be
    whitespace-free.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("apex_authority_ref must not be empty")
    if len(text) > 512:
        raise ValueError("apex_authority_ref must be at most 512 characters")
    if "\x00" in text or any(ord(character) < 32 for character in text):
        raise ValueError("apex_authority_ref must not contain control characters")
    if "://" in text:
        scheme, _, remainder = text.partition("://")
        if not scheme or not remainder:
            raise ValueError(
                "apex_authority_ref URL must include a scheme and a target"
            )
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
            raise ValueError("apex_authority_ref URL scheme is invalid")
        if any(character.isspace() for character in remainder):
            raise ValueError("apex_authority_ref URL must not contain whitespace")
        return text
    path_part, at, sha_part = text.rpartition("@")
    if not at or not path_part or not sha_part:
        raise ValueError(
            "apex_authority_ref must be a URL or a '<repo-path>@<git-sha>' ref"
        )
    try:
        _policy_path(path_part)
    except ValueError as exc:
        raise ValueError("apex_authority_ref repo path is invalid") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha_part):
        raise ValueError(
            "apex_authority_ref commit must be a 7-40 character git SHA"
        )
    return text


def _validate_ratified_spec_commit(value: object) -> str | None:
    """Validate an optional ``ProjectPolicy.ratified_spec_commit`` (M-GV-04).

    The ratified spec commit is a full 40-hex git SHA pinning the project's
    governing spec (the apex authority) to one immutable commit. Runtime jobs
    are spec-gated to it: a job whose spec is not pinned to this commit is
    rejected.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError("ratified_spec_commit must not be empty")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", text):
        raise ValueError("ratified_spec_commit must be a 40-character git SHA")
    return text


def _unexpected_path(value: str) -> str:
    """Validate one ``unexpected_paths`` entry (M-CC-06).

    Entries may be shell-style glob patterns (``*.lock``, ``**/*.lock``) or
    literal relative paths; a trailing ``/`` marks a directory prefix
    (``dist/``). They must be non-empty, relative, free of ``..`` traversal
    and NUL bytes, and at most 2000 characters. The normalized form (forward
    slashes, trailing directory marker preserved) is returned so the stored
    policy is deterministic.
    """
    normalized = value.replace("\\", "/").strip()
    if not normalized:
        raise ValueError("unexpected_paths entries must be non-empty")
    if normalized.startswith("/"):
        raise ValueError(
            "unexpected_paths entries must be non-empty relative paths without '..'"
        )
    if len(normalized) > 2000 or "\x00" in normalized:
        raise ValueError(
            "unexpected_paths entries must be NUL-free and at most 2000 characters"
        )
    if ".." in PurePosixPath(normalized).parts:
        raise ValueError(
            "unexpected_paths entries must be non-empty relative paths without '..'"
        )
    return normalized



# Names of interpreter/library loader variables a policy-configured command must
# not set, because they would let a runtime check rebind the interpreter or
# shared-library search path the bridge runs under (an injection vector). PATH
# is intentionally allowed so a host-runtime check can discover tools like
# ``docker``; the bridge never inherits its full process environment anyway.
_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "SHLIB_PATH",
        "LIBPATH",
        "PERL5LIB",
        "PERL5OPT",
        "RUBYOPT",
    }
)


def _validate_command_argv(value: object, *, label: str, exe_label: str) -> tuple[str, ...]:
    """Validate a fixed command argument vector shared by every command model.

    The argv must be a list of 1–64 non-empty, NUL-free, ≤2000-character
    arguments. ``{python}`` is allowed only as the executable (argv[0]) and is
    expanded to ``sys.executable`` at run time; a shell executable (sh, bash,
    ...) is rejected so no configured command can ever become a shell surface.
    ``label``/``exe_label`` parameterize the error messages so each model keeps
    its own wording (existing tests assert the exact text).
    """
    if isinstance(value, (str, bytes)) or value is None:
        raise ValueError(f"{label} argv must be a list of arguments")
    arguments = tuple(str(item) for item in value)  # type: ignore[arg-type]
    if not 1 <= len(arguments) <= 64:
        raise ValueError(f"{label} argv must contain 1 through 64 arguments")
    for argument in arguments:
        if not argument or len(argument) > 2000 or "\x00" in argument:
            raise ValueError(
                f"{label} arguments must be non-empty, NUL-free, and at "
                "most 2000 characters"
            )
    if "{python}" in arguments[1:]:
        raise ValueError(f"{{python}} is allowed only as the {exe_label} executable")
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
        raise ValueError(f"{label} commands must not invoke a shell")
    return arguments


def _validate_env_mapping(value: object) -> dict[str, str]:
    """Validate a policy-defined environment mapping for a runtime command.

    Keys must be uppercase ``[A-Z_][A-Z0-9_]*`` env names (≤128 chars) and not
    fall in the forbidden interpreter/loader set. Values must be NUL-free
    strings ≤4000 chars. Up to 64 entries. The bridge never returns these
    values wholesale (introspection redacts them); this only guards the shape.
    """
    if value is None:
        return {}
    if isinstance(value, (str, bytes)):
        raise ValueError("environment must be a mapping of string keys to string values")
    if not isinstance(value, dict):
        raise ValueError("environment must be a mapping of string keys to string values")
    if len(value) > 64:
        raise ValueError("environment may contain at most 64 entries")
    import re

    key_pattern = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if not key_pattern.match(key):
            raise ValueError(
                "environment keys must be uppercase [A-Z_][A-Z0-9_]* names"
            )
        if key in _FORBIDDEN_ENV_KEYS:
            raise ValueError(
                f"environment key {key!r} is forbidden (it would rebind the "
                "interpreter or shared-library search path)"
            )
        text = str(raw_value)
        if "\x00" in text or len(text) > 4000:
            raise ValueError(
                "environment values must be NUL-free and at most 4000 characters"
            )
        result[key] = text
    return result


class VerificationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...]
    python_path: tuple[str, ...] = ()
    sandbox: bool = False

    @field_validator("argv", mode="before")
    @classmethod
    def validate_argv(cls, value: object) -> tuple[str, ...]:
        return _validate_command_argv(value, label="verification", exe_label="verification")

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


class SupplyChainCommand(BaseModel):
    """One severity-gated supply-chain scan run at commit time (M-CC-11).

    A fixed argument vector for a deterministic scanner (e.g. ``grype ... --fail
    -on=high``, ``pip-audit ...``). The bridge runs it with fixed argv (no
    shell) inside the candidate worktree at commit time and rejects the commit
    if it exits non-zero (which is exactly how grype's ``--fail-on`` surfaces a
    HIGH/CRITICAL finding). Internal and opt-in per project; no new tool.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...]

    @field_validator("argv", mode="before")
    @classmethod
    def validate_argv(cls, value: object) -> tuple[str, ...]:
        return _validate_command_argv(value, label="supply-chain", exe_label="supply-chain")


class RuntimeVerificationCommand(BaseModel):
    """One policy-named runtime verification check (MCPB-011).

    A fixed argument vector for a check that must run against the project's
    canonical checkout with host-runtime access (Docker daemon, loopback
    networking, host-managed services, integration-test infrastructure). The
    MCP caller selects only the ``id``; the executable, argv, environment,
    working directory, and timeout are all defined here in trusted local policy
    and can never be supplied by the caller. Executed directly with
    ``asyncio.create_subprocess_exec`` (no shell). Separate from
    ``VerificationCommand``, which runs against an isolated candidate worktree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...]
    environment: dict[str, str] = Field(default_factory=dict)
    execution_mode: Literal["host"] = "host"
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)

    @field_validator("argv", mode="before")
    @classmethod
    def validate_argv(cls, value: object) -> tuple[str, ...]:
        return _validate_command_argv(
            value, label="runtime verification", exe_label="runtime verification"
        )

    @field_validator("environment", mode="before")
    @classmethod
    def validate_environment(cls, value: object) -> dict[str, str]:
        return _validate_env_mapping(value)


# Credential names that a project may not declare in ``credential_policy``
# because the bridge sets them deterministically for worktree subprocesses
# (M-CC-09 hermetic lab): projecting a bridge-environment value over the
# deterministic default would break reproducible verification.
_DETERMINISTIC_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "TZ",
        "SOURCE_DATE_EPOCH",
        "PYTHONHASHSEED",
    }
)


def _policy_allow_path(value: str) -> str:
    """Validate one filesystem/sensitivity allowlist entry.

    Mirrors ``_capture_scope_path`` (non-empty, relative, no ``..``, no empty
    or ``.`` segments) but additionally allows the recursive glob suffix
    ``**`` so a policy may declare a whole subtree (``docs/**``). ``**`` is
    permitted only as a trailing full segment (``a/**`` or ``a/**/b`` is
    rejected; only ``a/**`` is accepted) to keep matching semantics
    deterministic and one-dimensional.
    """
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "policy allowlist entries must be non-empty relative paths without '..'"
        )
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "**":
            if index != len(parts) - 1:
                raise ValueError("'**' is allowed only as the final path segment")
        elif part in ("", "."):
            raise ValueError(
                "policy allowlist entries must not contain empty or '.' segments"
            )
    return path.as_posix()


def _validate_allowlist(value: object, *, label: str) -> tuple[str, ...]:
    """Validate a tuple of policy allowlist entries (paths or globs)."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a list of relative paths")
    entries = tuple(_policy_allow_path(str(item)) for item in value)  # type: ignore[arg-type]
    if len(entries) != len(set(entries)):
        raise ValueError(f"{label} must not contain duplicate entries")
    return entries


def _capture_scope_path(value: str) -> str:
    """Validate one include/protected-exclude path for the capture scope.

    Mirrors ``_policy_path`` (non-empty, relative, no ``..``) but additionally
    allows the recursive glob suffix ``**`` so a policy may declare
    ``docs/**``. ``**`` is permitted only as a trailing full segment (``a/**``
    or ``a/**/b`` is rejected; only ``a/**`` is accepted) to keep glob
    semantics deterministic and one-dimensional.
    """
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "capture scope paths must be non-empty relative paths without '..'"
        )
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "**":
            if index != len(parts) - 1:
                raise ValueError("'**' is allowed only as the final path segment")
        elif part in ("", "."):
            raise ValueError("capture scope paths must not contain empty or '.' segments")
    return path.as_posix()


class WorkingTreeCaptureScope(BaseModel):
    """The approved capture scope for one project (MCPB-012).

    Pinned in trusted local policy (``projects.json``) so the MCP caller cannot
    widen, narrow, or guess the include/protected-exclude set. When the
    ``working_tree_candidate_capture`` capability is granted, the caller's
    include/protected-exclude paths must equal these exactly (as sets); the
    protected exclusions always override the inclusions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    include_paths: tuple[str, ...]
    protected_exclude_paths: tuple[str, ...] = ()

    @field_validator("include_paths", "protected_exclude_paths", mode="before")
    @classmethod
    def validate_paths(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("capture scope paths must be a list of strings")
        paths = tuple(_capture_scope_path(str(item)) for item in value)  # type: ignore[arg-type]
        if len(paths) != len(set(paths)):
            raise ValueError("capture scope paths must not contain duplicates")
        return paths

    @model_validator(mode="after")
    def validate_scope(self) -> "WorkingTreeCaptureScope":
        if not self.include_paths:
            raise ValueError("working_tree_capture_scope requires a non-empty include_paths")
        # A protected-exclude path that is also an include is allowed: the
        # protected exclusion wins and removes it from the manifest. But two
        # include paths must not overlap (one selecting a strict superset of the
        # other), because that would make the captured manifest ambiguous.
        return self


class OperationDefinition(BaseModel):
    """Detailed definition of a governed operation provided by a capability."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = Field(min_length=1, max_length=500)


class CapabilityDefinition(BaseModel):
    """Detailed registration of a capability and its available operations."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ProjectCapability
    description: str = Field(min_length=1, max_length=500)
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    protected_paths: tuple[str, ...] = ()
    commit_roots: tuple[str, ...] = ()
    verification_commands: tuple[VerificationCommand, ...] = ()
    lifecycle_requirements: str | None = None
    disposable_runtime: bool = False
    operations: dict[str, OperationDefinition] = Field(default_factory=dict)


class FileSystemPolicy(BaseModel):
    """Per-project filesystem allowlists for job operations (M-CC-11).

    Opt-in and validated. ``read_allow`` constrains which repository paths a
    job may read through the reader surface; ``write_allow`` constrains which
    paths a job may write. Entries are relative paths; a trailing ``/**``
    selects a whole subtree. An empty list on a side leaves that side
    unconstrained (the existing protected-path and approved write-root rules
    still apply). At least one side must be non-empty for the policy to be
    meaningful.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    read_allow: tuple[str, ...] = Field(default_factory=tuple)
    write_allow: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("read_allow", "write_allow", mode="before")
    @classmethod
    def validate_allowlists(cls, value: object) -> tuple[str, ...]:
        return _validate_allowlist(value, label="filesystem allowlist")

    @model_validator(mode="after")
    def validate_non_empty(self) -> "FileSystemPolicy":
        if not self.read_allow and not self.write_allow:
            raise ValueError(
                "filesystem_policy requires at least one read_allow or write_allow entry"
            )
        return self


class CredentialPolicy(BaseModel):
    """Least-privilege, short-lived credential projection (M-CC-11).

    Opt-in per project. Declares the ONLY credential environment-variable
    names the bridge may project from its own process environment into a job's
    worktree subprocess environment (verification commands, supply-chain
    scanners, and the Claude worktree launcher). No other credential from the
    bridge's environment is ever passed through; when the policy is unset the
    bridge keeps its existing minimal deterministic environment behavior.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("allow", mode="before")
    @classmethod
    def validate_allow(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError(
                "credential_policy.allow must be a list of environment variable names"
            )
        import re

        key_pattern = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
        names = tuple(str(item) for item in value)  # type: ignore[arg-type]
        if len(names) != len(set(names)):
            raise ValueError("credential_policy.allow must not contain duplicate names")
        for name in names:
            if not key_pattern.match(name):
                raise ValueError(
                    "credential names must be uppercase [A-Z_][A-Z0-9_]* names"
                )
            if name in _FORBIDDEN_ENV_KEYS:
                raise ValueError(
                    f"credential name {name!r} is forbidden (it would rebind the "
                    "interpreter or shared-library search path)"
                )
            if name in _DETERMINISTIC_ENV_KEYS:
                raise ValueError(
                    f"credential name {name!r} is reserved for the deterministic "
                    "verification environment"
                )
        return names

    @model_validator(mode="after")
    def validate_non_empty(self) -> "CredentialPolicy":
        if not self.allow:
            raise ValueError("credential_policy requires at least one allow entry")
        return self


class SensitivityPolicy(BaseModel):
    """Per-project sensitivity classification before evidence export (M-CC-11).

    Opt-in per project. Declares paths/patterns that must be withheld from the
    evidence bundle: any changed path or diff-detail line at or under a
    declared sensitive path is excluded from the bundle (in addition to the
    built-in global sensitive-path filter) and the exclusions are counted so a
    recipient can see that artifacts were withheld.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sensitive_paths: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("sensitive_paths", mode="before")
    @classmethod
    def validate_paths(cls, value: object) -> tuple[str, ...]:
        return _validate_allowlist(value, label="sensitivity_policy.sensitive_paths")

    @model_validator(mode="after")
    def validate_non_empty(self) -> "SensitivityPolicy":
        if not self.sensitive_paths:
            raise ValueError("sensitivity_policy requires at least one sensitive_path")
        return self


class ProjectPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    root: Path
    enabled: bool = True
    require_git: bool = True
    capabilities: frozenset[ProjectCapability]
    capability_definitions: dict[str, CapabilityDefinition] = Field(default_factory=dict)
    markdown_write_roots: tuple[str, ...] = ()
    commit_roots: tuple[str, ...] = ()
    verification_commands: tuple[VerificationCommand, ...] = ()
    supply_chain_scans: tuple[SupplyChainCommand, ...] = ()
    runtime_verification_commands: tuple[RuntimeVerificationCommand, ...] = ()
    working_tree_capture_scope: WorkingTreeCaptureScope | None = None
    # M-CC-11 security-hardening policies — all opt-in, validated in
    # ``validate_policy``, and internal (none of them introduces an MCP tool).
    filesystem_policy: FileSystemPolicy | None = None
    credential_policy: CredentialPolicy | None = None
    signed_manifests: bool = False
    sensitivity_policy: SensitivityPolicy | None = None
    protected_paths: tuple[str, ...] = (".git", ".env")
    # M-CC-06: unexpected-files policy. A tuple of shell-style glob patterns or
    # literal relative paths (``*.lock``, ``dist/``, ``node_modules/``) that a
    # candidate's changed set must never contain unless the path is explicitly
    # declared in the verified path list. Empty (the default) disables the
    # commit-gate check, which only ever fails closed when the policy declares
    # patterns.
    unexpected_paths: tuple[str, ...] = ()
    # M-CC-09: the hermetic verification lab, all opt-in per project so existing
    # projects keep the pre-M-CC-09 behavior until they opt in.
    # ``toolchain_pin``: absolute path to a pinned toolchain (a venv directory
    # or interpreter) used to run this project's verification commands instead
    # of the bridge's own interpreter.
    # ``deps_cache_dir``: absolute path to a read-only dependency cache the
    # project's verification commands may read (never write); enforced with a
    # sandbox read-only bind, so commands must be ``sandbox: true`` to use it.
    # ``network_policy``: "none" (commands get no network namespace — fail
    # closed) or "allowlist" (commands may reach only ``network_allowlist``
    # hostnames via the project's loopback proxy; all other DNS fails closed).
    # Both modes require ``sandbox: true`` commands because only the sandbox
    # can guarantee the isolation.
    toolchain_pin: str | None = None
    deps_cache_dir: str | None = None
    network_policy: Literal["none", "allowlist"] | None = None
    network_allowlist: tuple[str, ...] = ()
    # ``None`` means "inherit the global ``CLAUDE_DEFAULT_EFFORT``" rather than
    # pinning a per-project default, so a bridge-wide default actually takes
    # effect for projects that do not opt out of it.
    default_effort: Effort | None = None
    allow_ultracode: bool = True
    self_managed: bool = False
    requires_review: bool = False
    allow_bridge_reviewer: bool = False
    # M-GV-07: when True, a job that reaches COMPLETED automatically runs the
    # verification gate and the independent bridge-spawned review concurrently
    # (asyncio.gather) and records both, so a candidate is gated end-to-end
    # without an extra manual verification/review round-trip. Opt-in per
    # project; default off keeps existing projects' behavior unchanged.
    parallel_gates: bool = False
    claude_model: str | None = None
    claude_max_turns: int | None = Field(default=None, ge=1, le=200)
    # M-GV-03: external grounding for genesis/apex authority. An optional
    # reference (a URL, or a repo path + commit SHA) to the external spec the
    # project's apex authority is grounded in. When ``require_apex_grounding``
    # is set (per policy, or globally via REQUIRE_APEX_GROUNDING), a policy
    # without an ``apex_authority_ref`` is rejected, and the first job on the
    # project (its genesis job) requires the ref to be present and unchanged
    # since registration.
    apex_authority_ref: str | None = None
    require_apex_grounding: bool = False
    # M-GV-04: spec-gated runtime. When set, a full 40-hex git SHA pinning the
    # project's governing spec (the apex authority) to one immutable commit.
    # Runtime jobs are only allowed under that ratified commit: a job whose
    # spec is not pinned to it is rejected as spec drift. Composes with
    # M-GV-03: the apex authority is the spec; the ratified commit is the
    # pinned point.
    ratified_spec_commit: str | None = None

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

    @field_validator("unexpected_paths", mode="before")
    @classmethod
    def validate_unexpected_paths(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)):
            raise ValueError("unexpected_paths must be a list of pattern strings")
        patterns = tuple(_unexpected_path(str(item)) for item in value)  # type: ignore[arg-type]
        if len(patterns) != len(set(patterns)):
            raise ValueError("unexpected_paths must not contain duplicates")
        return patterns

    @field_validator("claude_model")
    @classmethod
    def validate_optional_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("claude_model must not be empty")
        return stripped

    @field_validator("apex_authority_ref", mode="before")
    @classmethod
    def validate_apex_authority_ref(cls, value: object) -> str | None:
        return _validate_apex_authority_ref(value)

    @field_validator("ratified_spec_commit", mode="before")
    @classmethod
    def validate_ratified_spec_commit(cls, value: object) -> str | None:
        return _validate_ratified_spec_commit(value)

    @field_validator("toolchain_pin", "deps_cache_dir", mode="before")
    @classmethod
    def validate_absolute_opt_in_path(cls, value: object) -> str | None:
        """Validate an absolute opt-in host path (M-CC-09).

        ``toolchain_pin`` and ``deps_cache_dir`` are host-side operator paths
        (a venv/interpreter and a dependency cache). They must be absolute and
        must not traverse ``..``; the value is canonicalized with
        ``Path.resolve()`` so the toolchain-pin digest is stable across runs.
        An empty string is treated as unset (None).
        """
        if value is None or value == "":
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "toolchain_pin and deps_cache_dir must be absolute paths"
            )
        normalized = path.resolve()
        if any(part == ".." for part in normalized.parts):
            raise ValueError(
                "toolchain_pin and deps_cache_dir must not traverse '..'"
            )
        return str(normalized)

    @field_validator("network_allowlist", mode="before")
    @classmethod
    def validate_network_allowlist(cls, value: object) -> tuple[str, ...]:
        """Validate a network-allowlist host set (M-CC-09).

        Entries are lowercase hostnames (letters, digits, ``-``, ``.``), each
        1-253 characters, de-duplicated. They are pinned in trusted policy —
        never caller-supplied.
        """
        if value is None:
            return ()
        entries = tuple(str(item).strip().lower() for item in value)
        if len(entries) != len(set(entries)):
            raise ValueError("network_allowlist must not contain duplicates")
        for entry in entries:
            if not entry or len(entry) > 253:
                raise ValueError(
                    "network_allowlist hosts must be non-empty and at most "
                    "253 characters"
                )
            if not all(character.isalnum() or character in "-." for character in entry):
                raise ValueError(
                    "network_allowlist hosts may contain only letters, digits, "
                    "'-' and '.'"
                )
            if any(
                not part or part.startswith("-") or part.endswith("-")
                for part in entry.split(".")
            ):
                raise ValueError("network_allowlist hosts must be valid hostnames")
        return entries

    @model_validator(mode="after")
    def validate_policy(self) -> "ProjectPolicy":
        if self.default_effort == "ultracode" and not self.allow_ultracode:
            raise ValueError("default_effort cannot be ultracode when it is disabled")
        if self.self_managed and ProjectCapability.MARKDOWN_WRITE in self.capabilities:
            raise ValueError(
                "self-managed projects cannot use direct markdown_write against the "
                "production checkout"
            )
        # M-GV-03: a project that opts into apex grounding must declare its
        # external authority reference; an ungrounded apex authority is exactly
        # the configuration the flag exists to forbid.
        if self.require_apex_grounding and self.apex_authority_ref is None:
            raise ValueError(
                "require_apex_grounding requires apex_authority_ref "
                "(external grounding for apex authority)"
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
        if (
            ProjectCapability.OPEN_PULL_REQUEST in self.capabilities
            and ProjectCapability.PUSH_DEVELOPMENT_COMMIT not in self.capabilities
        ):
            raise ValueError("open_pull_request requires push_development_commit")
        # M-AL-01 introspection capabilities: read-only observation gated on the
        # prerequisite mutation/read capability they introspect, so a project
        # cannot introspect a surface it is not already authorized to use.
        if (
            ProjectCapability.JOB_INTROSPECTION in self.capabilities
            and ProjectCapability.CLAUDE_EDIT not in self.capabilities
        ):
            raise ValueError("job_introspection requires claude_edit")
        if (
            ProjectCapability.POLICY_INTROSPECTION in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError("policy_introspection requires repository_read")
        if (
            ProjectCapability.CANONICAL_HISTORY_READ in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError("canonical_history_read requires repository_read")
        # MCPB-011: runtime verification needs repository_read for canonical
        # git-state inspection, and a project opting in must configure at least
        # one named check so the capability is never granted empty (a capability
        # with nothing to select would be misleading authority).
        if (
            ProjectCapability.RUNTIME_VERIFICATION in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError("runtime_verification requires repository_read")
        if (
            ProjectCapability.RUNTIME_VERIFICATION in self.capabilities
            and not self.runtime_verification_commands
        ):
            raise ValueError(
                "runtime_verification requires at least one runtime verification "
                "command"
            )
        # MCPB-012: working-tree candidate capture needs repository_read for
        # read-only canonical working-tree inspection, and a project opting in
        # must pin its approved include/protected-exclude scope in trusted
        # policy so the MCP caller can never widen, narrow, or guess it (fail
        # closed; a capability with no pinned scope would let the caller pick
        # any paths, which is exactly the authority the threat model forbids).
        if (
            ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE in self.capabilities
            and ProjectCapability.REPOSITORY_READ not in self.capabilities
        ):
            raise ValueError(
                "working_tree_candidate_capture requires repository_read"
            )
        if (
            ProjectCapability.WORKING_TREE_CANDIDATE_CAPTURE in self.capabilities
            and self.working_tree_capture_scope is None
        ):
            raise ValueError(
                "working_tree_candidate_capture requires a pinned "
                "working_tree_capture_scope in policy"
            )
        # M-CC-06: the unexpected_paths entries are validated and normalized by
        # the field validator (non-empty, relative, no '..', no duplicates);
        # re-validate here so policy validation is a single, explicit gate.
        for pattern in self.unexpected_paths:
            _unexpected_path(pattern)

        # M-CC-11: signed_manifests and sensitivity_classification only mean
        # something when the evidence bundle can actually be exported; each is
        # a project opt-in gated on the capability it hardens (a signing or
        # classification policy on a project that can never export would be
        # misleading authority). Both are internal — no new tool.
        if (
            self.signed_manifests
            and ProjectCapability.EXPORT_EVIDENCE_BUNDLE not in self.capabilities
        ):
            raise ValueError("signed_manifests requires export_evidence_bundle")
        if (
            self.sensitivity_policy is not None
            and ProjectCapability.EXPORT_EVIDENCE_BUNDLE not in self.capabilities
        ):
            raise ValueError("sensitivity_policy requires export_evidence_bundle")
        command_ids = [command.id for command in self.verification_commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("verification command IDs must be unique per project")
        scan_ids = [scan.id for scan in self.supply_chain_scans]
        if len(scan_ids) != len(set(scan_ids)):
            raise ValueError("supply-chain scan IDs must be unique per project")
        runtime_ids = [command.id for command in self.runtime_verification_commands]
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError(
                "runtime verification command IDs must be unique per project"
            )
        # M-CC-09: the hermetic verification lab is a coherent opt-in — an
        # allowlist is meaningless without the allowlist mode, and the "none"
        # mode must not smuggle a host allowlist. Fail closed on inconsistency.
        if self.network_policy == "allowlist" and not self.network_allowlist:
            raise ValueError(
                "network_policy 'allowlist' requires a non-empty network_allowlist"
            )
        if self.network_policy == "none" and self.network_allowlist:
            raise ValueError(
                "network_policy 'none' must not configure a network_allowlist"
            )
        if self.network_policy is None and self.network_allowlist:
            raise ValueError(
                "network_allowlist requires network_policy 'allowlist'"
            )
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

ProjectPolicy.model_rebuild()


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
