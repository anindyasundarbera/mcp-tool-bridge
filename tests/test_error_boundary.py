"""Tests for the domain error boundary (``local_dev_mcp.errors``).

Covers the hard constraint that ``errors.py`` never imports ``fastmcp``, the
stable envelope/exception contract, the full bridge-exception → envelope
mapping, ``__cause__`` preservation, ``raise_as_interface_error``, and
path/secret redaction across error messages and details.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from local_dev_mcp import errors
from local_dev_mcp.errors import (
    ConradDomainError,
    DomainErrorEnvelope,
    ErrorFamily,
    RequirementError,
    normalize_exception,
    raise_as_interface_error,
    redact_text,
)

from local_dev_mcp.authorization import AuthorizationError
from local_dev_mcp.evidence import EvidenceBundleError
from local_dev_mcp.git import GitCommandError
from local_dev_mcp.jobs import DevelopmentJobError
from local_dev_mcp.open_hands_action import OpenHandsActionError
from local_dev_mcp.pr import PullRequestError
from local_dev_mcp.reader import RepositoryReadError
from local_dev_mcp.registry import ProjectRegistryError
from local_dev_mcp.requirement import RequirementManagerError
from local_dev_mcp.review import ReviewError
from local_dev_mcp.runtime_verification import RuntimeVerificationError
from local_dev_mcp.store import StateStoreError
from local_dev_mcp.sync import SyncError
from local_dev_mcp.verification import PublicationError, VerificationError
from local_dev_mcp.worktrees import WorktreeError
from local_dev_mcp.working_tree_capture import WorkingTreeCaptureError
from local_dev_mcp.writer import RepositoryWriteError


_ERRORS_MODULE_PATH = Path(errors.__file__).resolve()


class ErrorFamilyTests(unittest.TestCase):
    def test_declares_all_ten_families(self) -> None:
        expected = {
            "POLICY_DENIED",
            "VALIDATION_FAILED",
            "NOT_FOUND",
            "CONFLICT_OR_STALE",
            "VERIFICATION_FAILED",
            "REVIEW_FAILED",
            "AUTHORIZATION_FAILED",
            "PUBLICATION_FAILED",
            "CAPTURE_FAILED",
            "RUNTIME_ERROR",
        }
        self.assertEqual(set(ErrorFamily.__members__), expected)

    def test_members_are_stable_string_values(self) -> None:
        self.assertEqual(ErrorFamily.POLICY_DENIED.value, "policy_denied")
        self.assertEqual(ErrorFamily.VALIDATION_FAILED.value, "validation_failed")
        self.assertEqual(ErrorFamily.NOT_FOUND.value, "not_found")
        self.assertEqual(ErrorFamily.CONFLICT_OR_STALE.value, "conflict_or_stale")
        self.assertEqual(ErrorFamily.VERIFICATION_FAILED.value, "verification_failed")
        self.assertEqual(ErrorFamily.REVIEW_FAILED.value, "review_failed")
        self.assertEqual(ErrorFamily.AUTHORIZATION_FAILED.value, "authorization_failed")
        self.assertEqual(ErrorFamily.PUBLICATION_FAILED.value, "publication_failed")
        self.assertEqual(ErrorFamily.CAPTURE_FAILED.value, "capture_failed")
        self.assertEqual(ErrorFamily.RUNTIME_ERROR.value, "runtime_error")
        self.assertEqual(str(ErrorFamily.RUNTIME_ERROR), "runtime_error")


class FastMCPImportBoundaryTests(unittest.TestCase):
    """``errors.py`` must never import ``fastmcp``."""

    def test_errors_module_never_imports_fastmcp(self) -> None:
        source = _ERRORS_MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name,
                        "fastmcp",
                        "errors.py must never import fastmcp",
                    )
                    self.assertFalse(
                        alias.name.startswith("fastmcp."),
                        "errors.py must never import fastmcp submodules",
                    )
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    node.module,
                    "fastmcp",
                    "errors.py must never import fastmcp",
                )
                self.assertFalse(
                    (node.module or "").startswith("fastmcp."),
                    "errors.py must never import fastmcp submodules",
                )


class DomainErrorEnvelopeTests(unittest.TestCase):
    def test_to_dict_serializes_all_fields(self) -> None:
        envelope = DomainErrorEnvelope(
            code="repository_read",
            family=ErrorFamily.POLICY_DENIED,
            message="denied",
            remediation="fix it",
            retryable=False,
            user_error=True,
            details={"exception_type": "RepositoryReadError"},
        )
        self.assertEqual(
            envelope.to_dict(),
            {
                "code": "repository_read",
                "family": "policy_denied",
                "message": "denied",
                "remediation": "fix it",
                "retryable": False,
                "user_error": True,
                "details": {"exception_type": "RepositoryReadError"},
            },
        )

    def test_details_defaults_to_empty_dict(self) -> None:
        envelope = DomainErrorEnvelope(
            code="x",
            family=ErrorFamily.RUNTIME_ERROR,
            message="m",
            remediation="r",
            retryable=True,
            user_error=False,
        )
        self.assertEqual(envelope.details, {})

    def test_remediation_defaults_to_empty_string(self) -> None:
        envelope = DomainErrorEnvelope(
            code="x",
            family=ErrorFamily.RUNTIME_ERROR,
            message="m",
        )
        self.assertEqual(envelope.remediation, "")
        self.assertFalse(envelope.retryable)
        self.assertTrue(envelope.user_error)

    def test_envelope_is_frozen(self) -> None:
        envelope = DomainErrorEnvelope(
            code="x",
            family=ErrorFamily.RUNTIME_ERROR,
            message="m",
            remediation="r",
            retryable=True,
            user_error=False,
        )
        with self.assertRaises(Exception):
            envelope.message = "changed"  # type: ignore[misc]


class ConradDomainErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.envelope = DomainErrorEnvelope(
            code="authorization",
            family=ErrorFamily.AUTHORIZATION_FAILED,
            message="authorization expired",
            remediation="refresh it",
            retryable=False,
            user_error=True,
            details={"exception_type": "AuthorizationError"},
        )
        self.error = ConradDomainError(self.envelope)

    def test_is_an_exception_with_the_message(self) -> None:
        self.assertIsInstance(self.error, Exception)
        self.assertEqual(str(self.error), "authorization expired")

    def test_exposes_all_envelope_fields(self) -> None:
        self.assertEqual(self.error.code, "authorization")
        self.assertEqual(self.error.family, ErrorFamily.AUTHORIZATION_FAILED)
        self.assertEqual(self.error.message, "authorization expired")
        self.assertEqual(self.error.remediation, "refresh it")
        self.assertFalse(self.error.retryable)
        self.assertTrue(self.error.user_error)
        self.assertEqual(self.error.details, {"exception_type": "AuthorizationError"})

    def test_to_dict_matches_envelope(self) -> None:
        self.assertEqual(self.error.to_dict(), self.envelope.to_dict())


class RedactTextTests(unittest.TestCase):
    def test_redacts_paths(self) -> None:
        text = "failed to read /home/user/project/docs/a.md for the user"
        result = redact_text(text, redact_paths=[Path("/home/user/project")])
        self.assertEqual(
            result, "failed to read <redacted-path>/docs/a.md for the user"
        )

    def test_longer_paths_redacted_before_shorter_prefixes(self) -> None:
        text = "path /var/lib/repo/sub/file and /var/lib/repo"
        result = redact_text(
            text,
            redact_paths=[Path("/var/lib/repo"), Path("/var/lib/repo/sub")],
        )
        self.assertEqual(
            result, "path <redacted-path>/file and <redacted-path>"
        )

    def test_redacts_secrets(self) -> None:
        text = "token=super-secret-token was rejected"
        result = redact_text(text, redact_secrets=["super-secret-token"])
        self.assertEqual(result, "token=<redacted-secret> was rejected")

    def test_longer_secrets_redacted_before_shorter_substrings(self) -> None:
        text = "secret: token-123456 and token-123"
        result = redact_text(text, redact_secrets=["token-123", "token-123456"])
        self.assertEqual(result, "secret: <redacted-secret> and <redacted-secret>")

    def test_redacts_paths_and_secrets_together(self) -> None:
        text = "wrote /data/repo using key=abc123xyz"
        result = redact_text(
            text,
            redact_paths=[Path("/data/repo")],
            redact_secrets=["abc123xyz"],
        )
        self.assertEqual(result, "wrote <redacted-path> using key=<redacted-secret>")

    def test_ignores_empty_paths_and_secrets(self) -> None:
        text = "unchanged"
        result = redact_text(text, redact_paths=[Path("")], redact_secrets=[""])
        self.assertEqual(result, "unchanged")

    def test_returns_input_when_nothing_to_redact(self) -> None:
        text = "plain message"
        self.assertEqual(redact_text(text), "plain message")


class NormalizeExceptionMappingTests(unittest.TestCase):
    """The complete bridge-exception → envelope mapping."""

    # (exception class, code, family, retryable, user_error)
    CASES: tuple[tuple[type[BaseException], str, ErrorFamily, bool, bool], ...] = (
        (ProjectRegistryError, "project_registry", ErrorFamily.POLICY_DENIED, False, True),
        (RepositoryReadError, "repository_read", ErrorFamily.POLICY_DENIED, False, True),
        (RepositoryWriteError, "repository_write", ErrorFamily.POLICY_DENIED, False, True),
        (DevelopmentJobError, "development_job", ErrorFamily.VALIDATION_FAILED, False, True),
        (WorktreeError, "worktree", ErrorFamily.RUNTIME_ERROR, True, False),
        (VerificationError, "verification", ErrorFamily.VERIFICATION_FAILED, False, True),
        (ReviewError, "review", ErrorFamily.REVIEW_FAILED, False, True),
        (PublicationError, "publication", ErrorFamily.PUBLICATION_FAILED, False, True),
        (SyncError, "sync", ErrorFamily.CONFLICT_OR_STALE, True, True),
        (AuthorizationError, "authorization", ErrorFamily.AUTHORIZATION_FAILED, False, True),
        (EvidenceBundleError, "evidence_bundle", ErrorFamily.CAPTURE_FAILED, False, True),
        (PullRequestError, "pull_request", ErrorFamily.PUBLICATION_FAILED, False, True),
        (
            RuntimeVerificationError,
            "runtime_verification",
            ErrorFamily.VERIFICATION_FAILED,
            False,
            True,
        ),
        (
            WorkingTreeCaptureError,
            "working_tree_capture",
            ErrorFamily.CAPTURE_FAILED,
            False,
            True,
        ),
        (OpenHandsActionError, "open_hands_action", ErrorFamily.CAPTURE_FAILED, False, True),
        (RequirementManagerError, "requirement", ErrorFamily.RUNTIME_ERROR, False, False),
        (RequirementError, "requirement", ErrorFamily.RUNTIME_ERROR, False, False),
        (GitCommandError, "git_command", ErrorFamily.RUNTIME_ERROR, True, False),
        (StateStoreError, "state_store", ErrorFamily.RUNTIME_ERROR, False, False),
        (ValueError, "validation", ErrorFamily.VALIDATION_FAILED, False, True),
    )

    def test_every_bridge_exception_maps_correctly(self) -> None:
        for cls, code, family, retryable, user_error in self.CASES:
            with self.subTest(exception=cls.__name__):
                exc = cls("boom")
                normalized = normalize_exception(exc)
                self.assertIsInstance(normalized, ConradDomainError)
                self.assertEqual(normalized.code, code)
                self.assertEqual(normalized.family, family)
                self.assertEqual(normalized.retryable, retryable)
                self.assertEqual(normalized.user_error, user_error)
                self.assertEqual(normalized.message, "boom")
                self.assertTrue(len(normalized.remediation) > 0)
                self.assertEqual(
                    normalized.details, {"exception_type": cls.__name__}
                )

                # Check to_dict() serialization
                envelope_dict = normalized.to_dict()
                self.assertEqual(envelope_dict["code"], code)
                self.assertEqual(envelope_dict["family"], family.value)
                self.assertEqual(envelope_dict["retryable"], retryable)
                self.assertEqual(envelope_dict["user_error"], user_error)

    def test_requirement_error_alias_is_accessible(self) -> None:
        self.assertIs(RequirementError, RequirementManagerError)

    def test_value_error_subclasses_are_matched_before_bare_value_error(self) -> None:
        # RepositoryReadError/RepositoryWriteError subclass ValueError; their
        # specific mapping must win over the bare ValueError entry.
        normalized = normalize_exception(RepositoryReadError("denied"))
        self.assertEqual(normalized.code, "repository_read")
        self.assertEqual(normalized.family, ErrorFamily.POLICY_DENIED)

        normalized_write = normalize_exception(RepositoryWriteError("read-only"))
        self.assertEqual(normalized_write.code, "repository_write")
        self.assertEqual(normalized_write.family, ErrorFamily.POLICY_DENIED)

    def test_unexpected_exception_is_runtime_and_not_user_error(self) -> None:
        normalized = normalize_exception(RuntimeError("mystery"))
        self.assertEqual(normalized.family, ErrorFamily.RUNTIME_ERROR)
        self.assertFalse(normalized.user_error)
        self.assertTrue(normalized.retryable)
        self.assertEqual(normalized.code, "internal")

    def test_state_store_error_is_not_user_error(self) -> None:
        normalized = normalize_exception(StateStoreError("db locked"))
        self.assertEqual(normalized.family, ErrorFamily.RUNTIME_ERROR)
        self.assertFalse(normalized.user_error)

    def test_is_idempotent_for_already_normalized_errors_without_redaction(self) -> None:
        first = normalize_exception(RepositoryReadError("denied"))
        second = normalize_exception(first)
        self.assertIs(first, second)

    def test_re_redacts_already_normalized_error_when_redaction_supplied(self) -> None:
        first = normalize_exception(RepositoryReadError("failed at /var/data/repo with key secret123"))
        second = normalize_exception(
            first,
            redact_paths=[Path("/var/data/repo")],
            redact_secrets=["secret123"],
        )
        self.assertIsNot(first, second)
        self.assertEqual(second.message, "failed at <redacted-path> with key <redacted-secret>")
        self.assertEqual(second.code, first.code)
        self.assertEqual(second.family, first.family)
        self.assertEqual(second.user_error, first.user_error)
        self.assertIs(second.__cause__, first.__cause__)


class NormalizeExceptionRedactionTests(unittest.TestCase):
    def test_message_is_redacted_for_paths_and_secrets(self) -> None:
        exc = VerificationError("failed at /home/u/repo with token=supersecret")
        normalized = normalize_exception(
            exc,
            redact_paths=[Path("/home/u/repo")],
            redact_secrets=["supersecret"],
        )
        self.assertEqual(
            normalized.message,
            "failed at <redacted-path> with token=<redacted-secret>",
        )

    def test_details_and_metadata_are_redacted_guaranteeing_no_leaks(self) -> None:
        class DetailedError(RuntimeError):
            pass

        exc = DetailedError("error in /secret/path/to/repo")
        exc.details = {  # type: ignore[attr-defined]
            "abs_path": Path("/secret/path/to/repo"),
            "token": "ghp_topsecrettoken123456",
            "nested": {
                "inner_path": "/secret/path/to/repo/file.txt",
                "nested_token": "ghp_topsecrettoken123456",
            },
            "list": ["/secret/path/to/repo/item", "ghp_topsecrettoken123456"],
        }

        secret_path = Path("/secret/path/to/repo")
        secret_token = "ghp_topsecrettoken123456"

        normalized = normalize_exception(
            exc,
            redact_paths=[secret_path],
            redact_secrets=[secret_token],
        )

        serialized = str(normalized.to_dict())

        # Assert no absolute paths or secrets leak in message, remediation, or details
        self.assertNotIn(str(secret_path), serialized)
        self.assertNotIn(secret_token, serialized)
        self.assertIn("<redacted-path>", serialized)
        self.assertIn("<redacted-secret>", serialized)

    def test_preserves_cause(self) -> None:
        exc = ReviewError("bad review")
        normalized = normalize_exception(exc)
        self.assertIs(normalized.__cause__, exc)


class RaiseAsInterfaceErrorTests(unittest.TestCase):
    def test_raises_value_error_with_normalized_message(self) -> None:
        exc = RepositoryReadError("policy denied")
        with self.assertRaises(ValueError) as ctx:
            raise_as_interface_error(exc)
        self.assertEqual(str(ctx.exception), "policy denied")

    def test_raises_from_the_original_exception(self) -> None:
        exc = VerificationError("verification failed")
        with self.assertRaises(ValueError) as ctx:
            raise_as_interface_error(exc)
        self.assertIs(ctx.exception.__cause__, exc)

    def test_redacts_paths_and_secrets_in_raised_message(self) -> None:
        exc = PublicationError("push /data/repo with secret hunter2")
        with self.assertRaises(ValueError) as ctx:
            raise_as_interface_error(
                exc,
                redact_paths=[Path("/data/repo")],
                redact_secrets=["hunter2"],
            )
        self.assertEqual(
            str(ctx.exception),
            "push <redacted-path> with secret <redacted-secret>",
        )

    def test_returns_never(self) -> None:
        # A call that raises is annotated NoReturn; verify it cannot return by
        # confirming an exception always escapes.
        with self.assertRaises(ValueError):
            raise_as_interface_error(ValueError("nope"))


if __name__ == "__main__":
    unittest.main()
