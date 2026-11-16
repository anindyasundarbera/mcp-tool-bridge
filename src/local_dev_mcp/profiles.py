"""Named capability profiles and trusted-policy expansion (Phase 3).

Zero FastMCP imports.

A *capability profile* is a named, versioned bundle of ``ProjectCapability``
grants that operators can register a repository under, so a project's
authorization surface is picked from a reviewed catalog instead of being
hand-assembled per project. The five profiles are strictly additive
tiers (``readonly`` ⊂ ``develop`` ⊂ ``develop-verify`` ⊂
``reviewed-development`` ⊂ ``publish-candidate``).

``expand_capability_profile`` turns a profile plus a repository trust mode
into a deterministic, digest-bearing ``ExpandedProfilePolicy``:

* Untrusted repositories may only run contained (fail closed when
  containment is unavailable or an uncontained override is requested).
* Trusted repositories default to contained execution and may opt into
  unsandboxed execution explicitly.
* Overrides (``extra_capabilities`` / ``remove_capabilities``) must satisfy
  the exact ``ProjectPolicy`` dependency-closure rules — the same rules the
  registry enforces at load time, so a profile expansion can never produce a
  policy the bridge would reject (or an authority shape it would not grant).
* The ``policy_digest`` is a SHA-256 over the canonical JSON of the expanded
  policy, so any change to the effective authority surface is detectable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from .containment import ContainmentMode
from .models import ProjectCapability, ProjectPolicy, VerificationCommand

__all__ = [
    "PROFILE_CATALOG_VERSION",
    "CapabilityProfileDefinition",
    "ExpandedProfilePolicy",
    "ProfileName",
    "RepositoryTrustMode",
    "expand_capability_profile",
    "explain_capability_profile",
    "list_capability_profiles",
]

#: Version of the profile catalog itself. Every shipped profile definition
#: carries this as its ``version`` until a profile is independently revised.
PROFILE_CATALOG_VERSION = "1.0"

# Branch prefixes that would make a publication target a trunk branch are
# rejected (case-insensitively) so a profile can never be configured to push
# onto the default history.
_TRUNK_BRANCH_NAMES = (
    "main",
    "master",
    "trunk",
    "head",
    "refs/heads/main",
    "refs/heads/master",
    "refs/heads/trunk",
    "refs/heads/head",
)


class ProfileName(str, Enum):
    """The named capability profiles in the catalog."""

    READONLY = "readonly"
    DEVELOP = "develop"
    DEVELOP_VERIFY = "develop-verify"
    REVIEWED_DEVELOPMENT = "reviewed-development"
    PUBLISH_CANDIDATE = "publish-candidate"


class RepositoryTrustMode(str, Enum):
    """How much the operator trusts the repository's host environment."""

    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


@dataclass(frozen=True)
class CapabilityProfileDefinition:
    """One reviewed, named bundle of capability grants."""

    name: ProfileName
    version: str
    summary: str
    capabilities: tuple[ProjectCapability, ...]
    requires_write_roots: bool
    requires_verification_commands: bool
    commit_requires_review: bool
    commit_requires_human_approval: bool
    allows_publication: bool
    default_containment_mode: str


@dataclass(frozen=True)
class ExpandedProfilePolicy:
    """A profile expanded for one repository trust mode, ready to register.

    ``capabilities`` holds the sorted capability *values*; ``policy_dict`` is
    the canonical payload the ``policy_digest`` was computed over (exposed as
    an immutable mapping so a caller cannot tamper with it after the fact).
    """

    profile_name: ProfileName
    profile_version: str
    trust_mode: RepositoryTrustMode
    capabilities: tuple[str, ...]
    containment_mode: str
    commit_requires_review: bool
    commit_requires_human_approval: bool
    authority_summary: str
    policy_digest: str
    policy_dict: Mapping[str, Any]


_READONLY = CapabilityProfileDefinition(
    name=ProfileName.READONLY,
    version="1.0",
    summary=(
        "Read-only repository access: list, search, read, and outline "
        "repository Markdown, plus policy introspection. No write, commit, "
        "or publication authority."
    ),
    capabilities=(
        ProjectCapability.REPOSITORY_READ,
        ProjectCapability.POLICY_INTROSPECTION,
    ),
    requires_write_roots=False,
    requires_verification_commands=False,
    commit_requires_review=False,
    commit_requires_human_approval=False,
    allows_publication=False,
    default_containment_mode=ContainmentMode.BWRAP.value,
)

_DEVELOP = CapabilityProfileDefinition(
    name=ProfileName.DEVELOP,
    version="1.0",
    summary=(
        "Contained development: everything in readonly plus delegated Claude "
        "editing inside isolated job worktrees, job introspection, job "
        "pause, and candidate discard. No commit or publication authority."
    ),
    capabilities=(
        ProjectCapability.REPOSITORY_READ,
        ProjectCapability.POLICY_INTROSPECTION,
        ProjectCapability.MARKDOWN_WRITE,
        ProjectCapability.CLAUDE_EDIT,
        ProjectCapability.JOB_INTROSPECTION,
        ProjectCapability.PAUSE_DEVELOPMENT_JOB,
        ProjectCapability.DISCARD_CANDIDATE,
    ),
    requires_write_roots=True,
    requires_verification_commands=False,
    commit_requires_review=False,
    commit_requires_human_approval=False,
    allows_publication=False,
    default_containment_mode=ContainmentMode.BWRAP.value,
)

_DEVELOP_VERIFY = CapabilityProfileDefinition(
    name=ProfileName.DEVELOP_VERIFY,
    version="1.0",
    summary=(
        "Verified development: everything in develop plus deterministic "
        "verification, human-approved commit of reviewed paths, verification "
        "replay, and authorization revocation."
    ),
    capabilities=(
        ProjectCapability.REPOSITORY_READ,
        ProjectCapability.POLICY_INTROSPECTION,
        ProjectCapability.MARKDOWN_WRITE,
        ProjectCapability.CLAUDE_EDIT,
        ProjectCapability.JOB_INTROSPECTION,
        ProjectCapability.PAUSE_DEVELOPMENT_JOB,
        ProjectCapability.DISCARD_CANDIDATE,
        ProjectCapability.VERIFICATION_COMMIT,
        ProjectCapability.REPLAY_VERIFICATION,
        ProjectCapability.REVOKE_AUTHORIZATION,
    ),
    requires_write_roots=True,
    requires_verification_commands=True,
    commit_requires_review=False,
    commit_requires_human_approval=True,
    allows_publication=False,
    default_containment_mode=ContainmentMode.BWRAP.value,
)

_REVIEWED_DEVELOPMENT = CapabilityProfileDefinition(
    name=ProfileName.REVIEWED_DEVELOPMENT,
    version="1.0",
    summary=(
        "Review-gated development: everything in develop-verify plus "
        "independent candidate review, canonical history read, and "
        "evidence-bundle export; commits additionally require a recorded "
        "review."
    ),
    capabilities=(
        ProjectCapability.REPOSITORY_READ,
        ProjectCapability.POLICY_INTROSPECTION,
        ProjectCapability.MARKDOWN_WRITE,
        ProjectCapability.CLAUDE_EDIT,
        ProjectCapability.JOB_INTROSPECTION,
        ProjectCapability.PAUSE_DEVELOPMENT_JOB,
        ProjectCapability.DISCARD_CANDIDATE,
        ProjectCapability.VERIFICATION_COMMIT,
        ProjectCapability.REPLAY_VERIFICATION,
        ProjectCapability.REVOKE_AUTHORIZATION,
        ProjectCapability.CANDIDATE_REVIEW,
        ProjectCapability.CANONICAL_HISTORY_READ,
        ProjectCapability.EXPORT_EVIDENCE_BUNDLE,
    ),
    requires_write_roots=True,
    requires_verification_commands=True,
    commit_requires_review=True,
    commit_requires_human_approval=True,
    allows_publication=False,
    default_containment_mode=ContainmentMode.BWRAP.value,
)

_PUBLISH_CANDIDATE = CapabilityProfileDefinition(
    name=ProfileName.PUBLISH_CANDIDATE,
    version="1.0",
    summary=(
        "Publication-ready development: everything in reviewed-development "
        "plus opt-in publication of one verified commit to a new non-trunk "
        "branch."
    ),
    capabilities=(
        ProjectCapability.REPOSITORY_READ,
        ProjectCapability.POLICY_INTROSPECTION,
        ProjectCapability.MARKDOWN_WRITE,
        ProjectCapability.CLAUDE_EDIT,
        ProjectCapability.JOB_INTROSPECTION,
        ProjectCapability.PAUSE_DEVELOPMENT_JOB,
        ProjectCapability.DISCARD_CANDIDATE,
        ProjectCapability.VERIFICATION_COMMIT,
        ProjectCapability.REPLAY_VERIFICATION,
        ProjectCapability.REVOKE_AUTHORIZATION,
        ProjectCapability.CANDIDATE_REVIEW,
        ProjectCapability.CANONICAL_HISTORY_READ,
        ProjectCapability.EXPORT_EVIDENCE_BUNDLE,
        ProjectCapability.PUSH_DEVELOPMENT_COMMIT,
    ),
    requires_write_roots=True,
    requires_verification_commands=True,
    commit_requires_review=True,
    commit_requires_human_approval=True,
    allows_publication=True,
    default_containment_mode=ContainmentMode.BWRAP.value,
)

#: The catalog, in tier order (each profile is a superset of the previous).
_PROFILE_DEFINITIONS: dict[ProfileName, CapabilityProfileDefinition] = {
    ProfileName.READONLY: _READONLY,
    ProfileName.DEVELOP: _DEVELOP,
    ProfileName.DEVELOP_VERIFY: _DEVELOP_VERIFY,
    ProfileName.REVIEWED_DEVELOPMENT: _REVIEWED_DEVELOPMENT,
    ProfileName.PUBLISH_CANDIDATE: _PUBLISH_CANDIDATE,
}


def list_capability_profiles() -> tuple[CapabilityProfileDefinition, ...]:
    """Return every profile definition in tier order."""
    return tuple(_PROFILE_DEFINITIONS[name] for name in ProfileName)


def _coerce_profile(profile: ProfileName | str) -> ProfileName:
    if isinstance(profile, ProfileName):
        return profile
    try:
        return ProfileName(str(profile))
    except ValueError as exc:
        raise ValueError(
            f"unknown capability profile: {profile!r}; choose one of "
            f"{', '.join(name.value for name in ProfileName)}"
        ) from exc


def _coerce_trust_mode(trust_mode: RepositoryTrustMode | str) -> RepositoryTrustMode:
    if isinstance(trust_mode, RepositoryTrustMode):
        return trust_mode
    try:
        return RepositoryTrustMode(str(trust_mode))
    except ValueError as exc:
        raise ValueError(
            f"unknown repository trust mode: {trust_mode!r}; choose one of "
            f"{', '.join(mode.value for mode in RepositoryTrustMode)}"
        ) from exc


def _coerce_capabilities(
    label: str, values: Sequence[ProjectCapability | str]
) -> set[ProjectCapability]:
    coerced: set[ProjectCapability] = set()
    for value in values:
        if isinstance(value, ProjectCapability):
            coerced.add(value)
            continue
        try:
            coerced.add(ProjectCapability(str(value)))
        except ValueError as exc:
            raise ValueError(f"unknown capability in {label}: {value!r}") from exc
    return coerced


def _validate_branch_prefix(branch_prefix: str) -> str:
    """Reject empty and trunk-targeting publication branch prefixes."""
    prefix = str(branch_prefix)
    if not prefix.strip():
        raise ValueError("branch_prefix must not be empty")
    if "\x00" in prefix or any(ord(character) < 32 for character in prefix):
        raise ValueError("branch_prefix must not contain control characters")
    if len(prefix) > 200:
        raise ValueError("branch_prefix must be at most 200 characters")
    lowered = prefix.strip().lower()
    for trunk in _TRUNK_BRANCH_NAMES:
        if lowered == trunk or lowered.startswith(trunk + "/"):
            raise ValueError(
                f"branch_prefix {branch_prefix!r} must not be empty or target a "
                f"trunk branch ({'/'.join(_TRUNK_BRANCH_NAMES)}); publication "
                "branches must be non-trunk"
            )
    return prefix


def _assert_dependency_closure(
    definition: CapabilityProfileDefinition,
    capabilities: frozenset[ProjectCapability],
) -> None:
    """Enforce the exact ``ProjectPolicy`` dependency-closure rules.

    Builds a synthetic (disabled, non-routable) ``ProjectPolicy`` carrying
    the final capability set plus the profile's review gate, provisioned
    with the minimum synthetic roots/commands the capability contract
    requires, and lets the production model validate the closure — the same
    rules the registry enforces, so there is a single source of truth and
    the check can never drift from the real authorization boundary.

    Capabilities whose configuration the profile system deliberately does
    not carry (runtime verification checks, working-tree capture scopes)
    fail closed here: a profile expansion can never grant them.
    """
    needs_write_roots = ProjectCapability.MARKDOWN_WRITE in capabilities
    needs_commands = ProjectCapability.VERIFICATION_COMMIT in capabilities
    synthetic_command = VerificationCommand.model_validate(
        {"id": "profile-closure-check", "argv": ("{python}", "-c", "pass")}
    )
    payload: dict[str, Any] = {
        "id": "profile-closure-check",
        "display_name": "Profile closure check",
        "description": "Synthetic policy used only to validate capability closure.",
        "root": "/",
        "enabled": False,
        "require_git": False,
        "capabilities": sorted(capability.value for capability in capabilities),
        "requires_review": definition.commit_requires_review and needs_commands,
        "markdown_write_roots": ["docs"] if needs_write_roots else [],
        "commit_roots": ["."] if needs_commands else [],
        "verification_commands": (
            [synthetic_command.model_dump(mode="json")] if needs_commands else []
        ),
    }
    try:
        ProjectPolicy.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            "capability profile overrides violate ProjectPolicy dependency "
            "closure: " + str(exc).replace("\n", " ")
        ) from exc


def _authority_summary(
    definition: CapabilityProfileDefinition,
    trust_mode: RepositoryTrustMode,
    capabilities: frozenset[ProjectCapability],
    containment_mode: str,
    branch_prefix: str,
) -> str:
    names = sorted(capability.value for capability in capabilities)
    parts: list[str] = [
        f"capability profile '{definition.name.value}' v{definition.version} "
        f"expanded for a {trust_mode.value} repository",
        (
            f"grants {len(names)} capabilities: {', '.join(names)}"
            if names
            else "grants no capabilities"
        ),
    ]
    if ProjectCapability.VERIFICATION_COMMIT in capabilities:
        gates: list[str] = []
        if definition.commit_requires_review:
            gates.append("independent review")
        if definition.commit_requires_human_approval:
            gates.append("explicit human approval")
        parts.append(
            "commit authority: verified commit gated on "
            + (" and ".join(gates) if gates else "verification")
        )
    else:
        parts.append("commit authority: none")
    if ProjectCapability.PUSH_DEVELOPMENT_COMMIT in capabilities:
        parts.append(
            "publication authority: one verified commit pushed to a new "
            f"non-trunk branch under '{branch_prefix}'"
        )
    else:
        parts.append("publication authority: none")
    parts.append(f"containment mode: {containment_mode}")
    return "; ".join(parts) + "."


def expand_capability_profile(
    profile: ProfileName | str,
    *,
    trust_mode: RepositoryTrustMode | str = RepositoryTrustMode.UNTRUSTED,
    extra_capabilities: Sequence[ProjectCapability | str] = (),
    remove_capabilities: Sequence[ProjectCapability | str] = (),
    branch_prefix: str = "conrad/",
    containment_available: bool = True,
    allow_uncontained_override: bool = False,
) -> ExpandedProfilePolicy:
    """Expand a named profile into a deterministic, digest-bearing policy.

    Containment authority (fail closed):

    * UNTRUSTED requires active containment — unavailable containment or a
      requested uncontained override is rejected outright.
    * TRUSTED defaults to the profile's contained mode and may explicitly
      opt into unsandboxed execution; it fails closed when containment is
      unavailable and no override was requested.

    Overrides only narrow or widen within the ``ProjectPolicy`` dependency
    closure: removing an absent capability is a harmless no-op, while any
    final capability set that violates a dependency rule is rejected with a
    ``ValueError`` describing the exact rule.
    """
    name = _coerce_profile(profile)
    mode = _coerce_trust_mode(trust_mode)
    definition = _PROFILE_DEFINITIONS[name]
    prefix = _validate_branch_prefix(branch_prefix)

    if mode is RepositoryTrustMode.UNTRUSTED:
        if not containment_available or allow_uncontained_override:
            raise ValueError(
                "Untrusted repository profile requires active containment "
                "and cannot run unsandboxed"
            )
        containment_mode = definition.default_containment_mode
    elif allow_uncontained_override:
        containment_mode = ContainmentMode.TRUSTED_DEV_UNCONTAINED.value
    elif containment_available:
        containment_mode = definition.default_containment_mode
    else:
        raise ValueError(
            "Trusted repository profile defaults to contained execution, but "
            "containment is unavailable; pass allow_uncontained_override=True "
            "to run unsandboxed"
        )

    granted: set[ProjectCapability] = set(definition.capabilities)
    granted |= _coerce_capabilities("extra_capabilities", extra_capabilities)
    granted -= _coerce_capabilities("remove_capabilities", remove_capabilities)
    _assert_dependency_closure(definition, frozenset(granted))
    capabilities = tuple(sorted(capability.value for capability in granted))

    policy_dict: dict[str, Any] = {
        "profile_catalog_version": PROFILE_CATALOG_VERSION,
        "profile_name": name.value,
        "profile_version": definition.version,
        "trust_mode": mode.value,
        "capabilities": list(capabilities),
        "containment_mode": containment_mode,
        "commit_requires_review": definition.commit_requires_review,
        "commit_requires_human_approval": definition.commit_requires_human_approval,
        "allows_publication": ProjectCapability.PUSH_DEVELOPMENT_COMMIT in granted,
        "branch_prefix": prefix,
    }
    policy_digest = hashlib.sha256(
        json.dumps(policy_dict, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

    return ExpandedProfilePolicy(
        profile_name=name,
        profile_version=definition.version,
        trust_mode=mode,
        capabilities=capabilities,
        containment_mode=containment_mode,
        commit_requires_review=definition.commit_requires_review,
        commit_requires_human_approval=definition.commit_requires_human_approval,
        authority_summary=_authority_summary(
            definition, mode, frozenset(granted), containment_mode, prefix
        ),
        policy_digest=policy_digest,
        policy_dict=MappingProxyType(policy_dict),
    )


def explain_capability_profile(
    profile: ProfileName | str,
    *,
    trust_mode: RepositoryTrustMode | str = RepositoryTrustMode.UNTRUSTED,
) -> dict[str, Any]:
    """Explain a profile definition and its trust-mode consequences."""
    name = _coerce_profile(profile)
    mode = _coerce_trust_mode(trust_mode)
    definition = _PROFILE_DEFINITIONS[name]
    return {
        "profile": name.value,
        "profile_catalog_version": PROFILE_CATALOG_VERSION,
        "version": definition.version,
        "summary": definition.summary,
        "trust_mode": mode.value,
        "capabilities": sorted(
            capability.value for capability in definition.capabilities
        ),
        "requires_write_roots": definition.requires_write_roots,
        "requires_verification_commands": definition.requires_verification_commands,
        "commit_requires_review": definition.commit_requires_review,
        "commit_requires_human_approval": definition.commit_requires_human_approval,
        "allows_publication": definition.allows_publication,
        "default_containment_mode": definition.default_containment_mode,
        "containment_required": mode is RepositoryTrustMode.UNTRUSTED,
        "uncontained_override_allowed": mode is RepositoryTrustMode.TRUSTED,
    }
