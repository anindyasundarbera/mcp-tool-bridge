"""Domain error boundary for the MCP Tool Bridge.

This module is the single place where the bridge's many domain-specific
exception types are translated into a stable, machine-readable error envelope
that a model-facing interface can emit without leaking filesystem paths,
secrets, or internal state.

Deliberate constraints (see ``tests/test_error_boundary.py``):

* This module must **never** import ``fastmcp`` (neither top-level nor lazily).
  It is a plain data layer with no transport dependency, so it can be unit-tested
  and reused by any interface without dragging in the server runtime.
* The exception classes are imported lazily inside :func:`_bridge_exception_mapping`
  so this module stays dependency-light and cannot participate in an import
  cycle with the modules that *define* those exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, NoReturn

if TYPE_CHECKING:
    from .requirement import RequirementManagerError as RequirementError


class ErrorFamily(StrEnum):
    """The coarse category of a domain error, stable across releases.

    Every :class:`DomainErrorEnvelope` carries exactly one family so callers
    can branch on the *kind* of failure without depending on the exact
    exception class that produced it.
    """

    POLICY_DENIED = "policy_denied"
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    CONFLICT_OR_STALE = "conflict_or_stale"
    VERIFICATION_FAILED = "verification_failed"
    REVIEW_FAILED = "review_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    PUBLICATION_FAILED = "publication_failed"
    CAPTURE_FAILED = "capture_failed"
    RUNTIME_ERROR = "runtime_error"


@dataclass(frozen=True)
class DomainErrorEnvelope:
    """A frozen, JSON-serializable description of a domain failure.

    ``code`` is the machine-readable identifier; ``family`` the coarse
    category; ``message`` the redacted, user-facing description;
    ``remediation`` actionable guidance; ``retryable`` whether retrying the
    same operation may help; ``user_error`` whether the failure is the
    caller's to fix (as opposed to internal/infrastructure); and ``details`` a
    small structured payload of non-sensitive metadata.
    """

    code: str
    family: ErrorFamily
    message: str
    remediation: str = ""
    retryable: bool = False
    user_error: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Render the envelope as a plain dict (``family`` as its string value)."""
        return {
            "code": self.code,
            "family": self.family.value if isinstance(self.family, ErrorFamily) else str(self.family),
            "message": self.message,
            "remediation": self.remediation,
            "retryable": self.retryable,
            "user_error": self.user_error,
            "details": dict(self.details),
        }


class ConradDomainError(Exception):
    """A domain error that wraps a :class:`DomainErrorEnvelope`.

    All the envelope fields are re-exposed as read-only properties so a caller
    can handle a raised error uniformly, while ``str(error)`` remains the
    redacted human-readable message.
    """

    def __init__(self, envelope: DomainErrorEnvelope) -> None:
        self.envelope = envelope
        super().__init__(envelope.message)

    @property
    def code(self) -> str:
        return self.envelope.code

    @property
    def family(self) -> ErrorFamily:
        return self.envelope.family

    @property
    def message(self) -> str:
        return self.envelope.message

    @property
    def remediation(self) -> str:
        return self.envelope.remediation

    @property
    def retryable(self) -> bool:
        return self.envelope.retryable

    @property
    def user_error(self) -> bool:
        return self.envelope.user_error

    @property
    def details(self) -> dict[str, Any]:
        return self.envelope.details

    def to_dict(self) -> dict[str, Any]:
        return self.envelope.to_dict()


def redact_text(
    text: str,
    *,
    redact_paths: Iterable[Path] = (),
    redact_secrets: Iterable[str] = (),
) -> str:
    """Replace every occurrence of a projected path or secret with a marker.

    Paths become ``<redacted-path>`` and secrets become ``<redacted-secret>``,
    mirroring the placeholders already used by the verification and PR layers.
    Empty path/secret values are ignored so a blank entry cannot splinter the
    string. Longer matches are replaced first to prevent partial replacements.
    """
    if not isinstance(text, str):
        text = str(text)

    result = text
    # Sort paths descending by length to replace more specific paths first
    sorted_paths = sorted(
        (str(p) for p in redact_paths if p and str(p)),
        key=len,
        reverse=True,
    )
    for path_str in sorted_paths:
        result = result.replace(path_str, "<redacted-path>")

    # Sort secrets descending by length
    sorted_secrets = sorted(
        (str(s) for s in redact_secrets if s and str(s)),
        key=len,
        reverse=True,
    )
    for secret_str in sorted_secrets:
        result = result.replace(secret_str, "<redacted-secret>")

    return result


def _redact_value(
    value: Any,
    paths: tuple[Path, ...],
    secrets: tuple[str, ...],
) -> Any:
    """Recursively redact paths and secrets from strings, collections, and mappings."""
    if isinstance(value, str):
        return redact_text(value, redact_paths=paths, redact_secrets=secrets)
    if isinstance(value, Path):
        return redact_text(str(value), redact_paths=paths, redact_secrets=secrets)
    if isinstance(value, dict):
        return {
            (
                redact_text(str(k), redact_paths=paths, redact_secrets=secrets)
                if isinstance(k, (str, Path))
                else k
            ): _redact_value(v, paths, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, paths, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, paths, secrets) for item in value)
    return value


# Tuple shape: (exception class, code, family, retryable, user_error, remediation).
_BRIDGE_EXCEPTION_MAPPING: tuple[
    tuple[type[BaseException], str, ErrorFamily, bool, bool, str], ...
] | None = None


def _bridge_exception_mapping() -> tuple[
    tuple[type[BaseException], str, ErrorFamily, bool, bool, str], ...
]:
    """Return the ordered exception mapping, importing the classes lazily.

    Order matters: subclasses must precede their bases. ``RepositoryReadError``
    and ``RepositoryWriteError`` subclass ``ValueError``, so they are listed
    before the bare ``ValueError`` entry, which is itself listed last so it can
    only catch otherwise-unclassified value errors.
    """
    global _BRIDGE_EXCEPTION_MAPPING
    if _BRIDGE_EXCEPTION_MAPPING is not None:
        return _BRIDGE_EXCEPTION_MAPPING

    from .authorization import AuthorizationError
    from .evidence import EvidenceBundleError
    from .git import GitCommandError
    from .jobs import DevelopmentJobError
    from .open_hands_action import OpenHandsActionError
    from .pr import PullRequestError
    from .reader import RepositoryReadError
    from .registry import ProjectRegistryError
    from .requirement import RequirementManagerError
    from .review import ReviewError
    from .runtime_verification import RuntimeVerificationError
    from .store import StateStoreError
    from .sync import SyncError
    from .verification import PublicationError, VerificationError
    from .worktrees import WorktreeError
    from .working_tree_capture import WorkingTreeCaptureError
    from .writer import RepositoryWriteError

    _BRIDGE_EXCEPTION_MAPPING = (
        (
            ProjectRegistryError,
            "project_registry",
            ErrorFamily.POLICY_DENIED,
            False,
            True,
            "Review the project registry configuration and confirm the project "
            "is allowlisted and valid.",
        ),
        (
            RepositoryReadError,
            "repository_read",
            ErrorFamily.POLICY_DENIED,
            False,
            True,
            "Confirm the requested repository path is within the project's "
            "approved read scope.",
        ),
        (
            RepositoryWriteError,
            "repository_write",
            ErrorFamily.POLICY_DENIED,
            False,
            True,
            "Confirm the target path is beneath an approved write root.",
        ),
        (
            DevelopmentJobError,
            "development_job",
            ErrorFamily.VALIDATION_FAILED,
            False,
            True,
            "Correct the development job request parameters and retry.",
        ),
        (
            WorktreeError,
            "worktree",
            ErrorFamily.RUNTIME_ERROR,
            True,
            False,
            "The isolated worktree could not be created or trusted; retry or "
            "inspect the checkout state.",
        ),
        (
            VerificationError,
            "verification",
            ErrorFamily.VERIFICATION_FAILED,
            False,
            True,
            "The candidate could not be verified; review the verification "
            "output and correct the candidate.",
        ),
        (
            ReviewError,
            "review",
            ErrorFamily.REVIEW_FAILED,
            False,
            True,
            "The candidate review could not be completed; address the review "
            "verdict or constraints.",
        ),
        (
            PublicationError,
            "publication",
            ErrorFamily.PUBLICATION_FAILED,
            False,
            True,
            "The commit could not be published; verify the branch name and "
            "remote state.",
        ),
        (
            SyncError,
            "sync",
            ErrorFamily.CONFLICT_OR_STALE,
            True,
            True,
            "The canonical checkout is out of sync; update the checkout and "
            "retry.",
        ),
        (
            AuthorizationError,
            "authorization",
            ErrorFamily.AUTHORIZATION_FAILED,
            False,
            True,
            "Reissue or refresh the authorization for the requested operation.",
        ),
        (
            EvidenceBundleError,
            "evidence_bundle",
            ErrorFamily.CAPTURE_FAILED,
            False,
            True,
            "The evidence bundle could not be assembled; correct the evidence "
            "inputs.",
        ),
        (
            PullRequestError,
            "pull_request",
            ErrorFamily.PUBLICATION_FAILED,
            False,
            True,
            "The pull request could not be opened or updated; verify the "
            "source branch.",
        ),
        (
            RuntimeVerificationError,
            "runtime_verification",
            ErrorFamily.VERIFICATION_FAILED,
            False,
            True,
            "The runtime verification could not be executed; check the named "
            "check id and expected head.",
        ),
        (
            WorkingTreeCaptureError,
            "working_tree_capture",
            ErrorFamily.CAPTURE_FAILED,
            False,
            True,
            "The working-tree changes could not be captured; review the "
            "approved capture scope.",
        ),
        (
            OpenHandsActionError,
            "open_hands_action",
            ErrorFamily.CAPTURE_FAILED,
            False,
            True,
            "The governed OpenHands action could not be executed or verified.",
        ),
        (
            RequirementManagerError,
            "requirement",
            ErrorFamily.RUNTIME_ERROR,
            False,
            False,
            "Requirement traceability could not be completed; inspect the "
            "documentation inputs.",
        ),
        (
            GitCommandError,
            "git_command",
            ErrorFamily.RUNTIME_ERROR,
            True,
            False,
            "A Git command failed; verify the repository state and retry.",
        ),
        (
            StateStoreError,
            "state_store",
            ErrorFamily.RUNTIME_ERROR,
            False,
            False,
            "The durable state store could not be opened; inspect the state "
            "directory.",
        ),
        (
            ValueError,
            "validation",
            ErrorFamily.VALIDATION_FAILED,
            False,
            True,
            "The provided input is invalid; correct it and retry.",
        ),
    )
    return _BRIDGE_EXCEPTION_MAPPING


def normalize_exception(
    exc: BaseException,
    *,
    redact_paths: Iterable[Path] = (),
    redact_secrets: Iterable[str] = (),
) -> ConradDomainError:
    """Translate any bridge exception into a :class:`ConradDomainError`.

    The message and metadata are redacted against the projected paths and secrets
    before capture, so the resulting envelope is safe to surface. ``__cause__`` is
    set to the original exception so traceback chaining is preserved if the
    normalized error is later re-raised.

    ``StateStoreError`` and any otherwise-unclassified ``Exception`` map to
    ``user_error=False`` (internal failures), while the unknown-exception
    fallback is also ``retryable=True`` and ``ErrorFamily.RUNTIME_ERROR``.
    """
    paths = tuple(redact_paths)
    secrets = tuple(redact_secrets)

    if isinstance(exc, ConradDomainError):
        if not paths and not secrets:
            return exc
        new_msg = redact_text(exc.message, redact_paths=paths, redact_secrets=secrets)
        new_rem = (
            redact_text(exc.remediation, redact_paths=paths, redact_secrets=secrets)
            if exc.remediation
            else exc.remediation
        )
        new_details = _redact_value(exc.details, paths, secrets)
        new_envelope = DomainErrorEnvelope(
            code=exc.code,
            family=exc.family,
            message=new_msg,
            remediation=new_rem,
            retryable=exc.retryable,
            user_error=exc.user_error,
            details=new_details if isinstance(new_details, dict) else dict(exc.details),
        )
        res = ConradDomainError(new_envelope)
        res.__cause__ = exc.__cause__ or exc
        return res

    raw_message = str(exc) or type(exc).__name__
    message = redact_text(raw_message, redact_paths=paths, redact_secrets=secrets)

    code = "internal"
    family = ErrorFamily.RUNTIME_ERROR
    retryable = True
    user_error = False
    remediation = "An unexpected internal error occurred; retry or contact the operator."

    for cls, mapped_code, mapped_family, mapped_retryable, mapped_user_error, mapped_remediation in (
        _bridge_exception_mapping()
    ):
        if isinstance(exc, cls):
            code = mapped_code
            family = mapped_family
            retryable = mapped_retryable
            user_error = mapped_user_error
            remediation = mapped_remediation
            break

    # Extract any details attribute from exc if present
    exc_details: dict[str, Any] = {"exception_type": type(exc).__name__}
    if hasattr(exc, "details") and isinstance(exc.details, dict):
        exc_details.update(exc.details)

    redacted_details = _redact_value(exc_details, paths, secrets)

    envelope = DomainErrorEnvelope(
        code=code,
        family=family,
        message=message,
        remediation=redact_text(remediation, redact_paths=paths, redact_secrets=secrets),
        retryable=retryable,
        user_error=user_error,
        details=redacted_details if isinstance(redacted_details, dict) else exc_details,
    )
    normalized = ConradDomainError(envelope)
    normalized.__cause__ = exc
    return normalized


def raise_as_interface_error(
    exc: BaseException,
    *,
    redact_paths: Iterable[Path] = (),
    redact_secrets: Iterable[str] = (),
) -> NoReturn:
    """Normalize ``exc`` and raise a redacted ``ValueError`` chained to it.

    This is the transport-facing raise surface: the caller gets a plain
    ``ValueError`` whose message is the normalized (redacted) description, with
    ``__cause__`` preserved from the original exception.
    """
    normalized = normalize_exception(
        exc,
        redact_paths=redact_paths,
        redact_secrets=redact_secrets,
    )
    raise ValueError(normalized.message) from exc


def __getattr__(name: str) -> Any:
    if name == "RequirementError":
        from .requirement import RequirementManagerError

        return RequirementManagerError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConradDomainError",
    "DomainErrorEnvelope",
    "ErrorFamily",
    "RequirementError",
    "normalize_exception",
    "raise_as_interface_error",
    "redact_text",
]
