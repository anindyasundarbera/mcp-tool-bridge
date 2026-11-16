"""M2.5: caller identity resolution, principal authority, and promotion gates.

Covers ``identity.py`` (``CallerPrincipal`` invariants, transport-derived
``resolve_caller_principal``, executor/legacy minting, and the require-*
helpers) and the authority changes to ``authorization.py`` (executor/non-
authenticated grant rejection, issuer recording, and the reservation state
machine) and ``review.py`` (the two-gate promotion governance). No network; the
review promotion tests use in-memory grants and a minimal review manager.
"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from local_dev_mcp.authorization import (
    Authorization,
    AuthorizationError,
    AuthorizationManager,
    GrantReservation,
)
from local_dev_mcp.config import Settings
from local_dev_mcp.identity import (
    AuthProvenance,
    CallerPrincipal,
    PrincipalRole,
    make_executor_principal,
    make_legacy_principal,
    require_authenticated_principal,
    require_non_executor_principal,
    require_steward_principal,
    resolve_caller_principal,
)
from local_dev_mcp.models import ProjectPolicy
from local_dev_mcp.registry import ProjectLockManager
from local_dev_mcp.review import (
    ApprovalSummaryDTO,
    PromotionAuthorityTrace,
    ReviewDecision,
    ReviewError,
    ReviewManager,
)
from local_dev_mcp.transports import PeerTransportMetadata, TransportMode


def settings(base: Path) -> Settings:
    return Settings.from_env(
        {
            "PROJECTS_CONFIG_PATH": str(base / "projects.json"),
            "JOB_WORKTREE_ROOT": str(base / "jobs"),
        }
    )


def steward_principal() -> CallerPrincipal:
    return CallerPrincipal(
        principal_id="local:steward",
        provenance=AuthProvenance.LOCAL_SINGLE_USER,
        role=PrincipalRole.STEWARD,
        transport_mode="stdio",
        authenticated=True,
    )


def operator_principal() -> CallerPrincipal:
    return CallerPrincipal(
        principal_id="local:operator",
        provenance=AuthProvenance.LOCAL_SINGLE_USER,
        role=PrincipalRole.OPERATOR,
        transport_mode="stdio",
        authenticated=True,
    )


# ---------------------------------------------------------------------------
# CallerPrincipal invariants
# ---------------------------------------------------------------------------


class CallerPrincipalValidationTests(unittest.TestCase):
    def test_requires_non_empty_principal_id(self) -> None:
        for bad in ("", "   "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CallerPrincipal(
                    principal_id=bad,
                    provenance=AuthProvenance.LOCAL_SINGLE_USER,
                    role=PrincipalRole.STEWARD,
                )

    def test_anonymous_must_not_be_authenticated(self) -> None:
        with self.assertRaises(ValueError):
            CallerPrincipal(
                principal_id="anon",
                provenance=AuthProvenance.LEGACY_UNAUTHENTICATED,
                role=PrincipalRole.ANONYMOUS,
                authenticated=True,
            )

    def test_legacy_unauthenticated_must_not_be_authenticated(self) -> None:
        with self.assertRaises(ValueError):
            CallerPrincipal(
                principal_id="legacy",
                provenance=AuthProvenance.LEGACY_UNAUTHENTICATED,
                role=PrincipalRole.REVIEWER,
                authenticated=True,
            )

    def test_executor_requires_executor_internal_provenance(self) -> None:
        with self.assertRaises(ValueError):
            CallerPrincipal(
                principal_id="executor:x",
                provenance=AuthProvenance.MTLS_VERIFIED,
                role=PrincipalRole.EXECUTOR,
                authenticated=True,
            )

    def test_executor_internal_provenance_is_valid(self) -> None:
        principal = CallerPrincipal(
            principal_id="executor:a:b",
            provenance=AuthProvenance.EXECUTOR_INTERNAL,
            role=PrincipalRole.EXECUTOR,
            authenticated=True,
        )
        self.assertEqual(principal.role, PrincipalRole.EXECUTOR)


# ---------------------------------------------------------------------------
# resolve_caller_principal
# ---------------------------------------------------------------------------


class ResolveCallerPrincipalTests(unittest.TestCase):
    def test_stdio_resolves_local_steward(self) -> None:
        peer = PeerTransportMetadata(transport_mode=TransportMode.STDIO)
        principal = resolve_caller_principal(peer)
        self.assertEqual(principal.principal_id, "local:steward")
        self.assertEqual(principal.provenance, AuthProvenance.LOCAL_SINGLE_USER)
        self.assertEqual(principal.role, PrincipalRole.STEWARD)
        self.assertEqual(principal.transport_mode, "stdio")
        self.assertTrue(principal.authenticated)

    def test_local_http_loopback_resolves_steward(self) -> None:
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.LOCAL_HTTP, remote_addr="127.0.0.1"
        )
        principal = resolve_caller_principal(peer)
        self.assertEqual(principal.principal_id, "local:steward")
        self.assertEqual(principal.transport_mode, "http")
        self.assertTrue(principal.authenticated)

    def test_local_http_non_loopback_fails_closed(self) -> None:
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.LOCAL_HTTP, remote_addr="8.8.8.8"
        )
        with self.assertRaises(PermissionError):
            resolve_caller_principal(peer)

    def test_local_http_missing_remote_addr_resolves_steward(self) -> None:
        peer = PeerTransportMetadata(transport_mode=TransportMode.LOCAL_HTTP)
        principal = resolve_caller_principal(peer)
        self.assertEqual(principal.principal_id, "local:steward")
        self.assertEqual(principal.transport_mode, "http")
        self.assertTrue(principal.authenticated)

    def test_ngrok_mtls_verified_resolves_operator(self) -> None:
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.NGROK_MTLS,
            remote_addr="203.0.113.9",
            mtls_subject_dn="CN=operator,O=Example",
            mtls_cert_sha256="ab" * 32,
            mtls_verified=True,
        )
        principal = resolve_caller_principal(peer)
        self.assertEqual(principal.principal_id, "mtls:CN=operator,O=Example")
        self.assertEqual(principal.provenance, AuthProvenance.MTLS_VERIFIED)
        self.assertEqual(principal.role, PrincipalRole.OPERATOR)
        self.assertEqual(principal.transport_mode, "ngrok_mtls")
        self.assertEqual(principal.cert_fingerprint, "ab" * 32)
        self.assertTrue(principal.authenticated)

    def test_ngrok_mtls_unverified_fails_closed(self) -> None:
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.NGROK_MTLS,
            mtls_subject_dn="CN=operator,O=Example",
            mtls_cert_sha256="ab" * 32,
            mtls_verified=False,
        )
        with self.assertRaises(PermissionError):
            resolve_caller_principal(peer)

    def test_ngrok_mtls_missing_subject_fails_closed(self) -> None:
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.NGROK_MTLS,
            mtls_cert_sha256="ab" * 32,
            mtls_verified=True,
        )
        with self.assertRaises(PermissionError):
            resolve_caller_principal(peer)

    def test_none_peer_raises_without_legacy(self) -> None:
        with self.assertRaises(PermissionError):
            resolve_caller_principal(None)

    def test_none_peer_returns_legacy_when_allowed(self) -> None:
        principal = resolve_caller_principal(
            None, claimed_actor_label="old-caller", allow_legacy_unauthenticated=True
        )
        self.assertEqual(principal.provenance, AuthProvenance.LEGACY_UNAUTHENTICATED)
        self.assertEqual(principal.role, PrincipalRole.ANONYMOUS)
        self.assertFalse(principal.authenticated)
        self.assertEqual(principal.claimed_actor_label, "old-caller")

    def test_untrusted_headers_never_become_identity(self) -> None:
        # A spoofed identity header on loopback must not influence resolution.
        peer = PeerTransportMetadata(
            transport_mode=TransportMode.LOCAL_HTTP,
            remote_addr="127.0.0.1",
            untrusted_headers={"X-Client-Cert-SHA256": "deadbeef"},
        )
        principal = resolve_caller_principal(peer)
        self.assertEqual(principal.principal_id, "local:steward")
        self.assertIsNone(principal.cert_fingerprint)

    def test_spoofed_claimed_labels_never_elevate(self) -> None:
        for label in ("human", "steward", "chatgpt", "bridge"):
            with self.subTest(label=label):
                # When peer is None with allow_legacy_unauthenticated:
                principal = resolve_caller_principal(
                    None, claimed_actor_label=label, allow_legacy_unauthenticated=True
                )
                self.assertFalse(principal.authenticated)
                self.assertEqual(principal.role, PrincipalRole.ANONYMOUS)
                self.assertEqual(principal.provenance, AuthProvenance.LEGACY_UNAUTHENTICATED)
                self.assertEqual(principal.claimed_actor_label, label)

                # Over untrusted mTLS peer:
                unverified_peer = PeerTransportMetadata(
                    transport_mode=TransportMode.NGROK_MTLS,
                    mtls_verified=False,
                    untrusted_headers={"X-Actor": label, "X-Principal": label},
                )
                with self.assertRaises(PermissionError):
                    resolve_caller_principal(unverified_peer, claimed_actor_label=label)


class PrincipalMintingTests(unittest.TestCase):
    def test_make_executor_principal(self) -> None:
        principal = make_executor_principal("adapter-1", "exec-42")
        self.assertEqual(principal.principal_id, "executor:adapter-1:exec-42")
        self.assertEqual(principal.provenance, AuthProvenance.EXECUTOR_INTERNAL)
        self.assertEqual(principal.role, PrincipalRole.EXECUTOR)
        self.assertTrue(principal.authenticated)

    def test_make_legacy_principal(self) -> None:
        principal = make_legacy_principal("claimed-label")
        self.assertEqual(principal.principal_id, "legacy:unauthenticated")
        self.assertEqual(principal.provenance, AuthProvenance.LEGACY_UNAUTHENTICATED)
        self.assertEqual(principal.role, PrincipalRole.ANONYMOUS)
        self.assertFalse(principal.authenticated)
        self.assertEqual(principal.claimed_actor_label, "claimed-label")


class PrincipalRequireHelperTests(unittest.TestCase):
    def test_require_authenticated(self) -> None:
        self.assertEqual(require_authenticated_principal(steward_principal()).role, PrincipalRole.STEWARD)
        with self.assertRaises(PermissionError):
            require_authenticated_principal(None)
        with self.assertRaises(PermissionError):
            require_authenticated_principal(make_legacy_principal())

    def test_require_non_executor(self) -> None:
        require_non_executor_principal(steward_principal())
        with self.assertRaises(PermissionError):
            require_non_executor_principal(make_executor_principal("a", "e"))
        with self.assertRaises(PermissionError):
            require_non_executor_principal(make_legacy_principal())

    def test_require_steward(self) -> None:
        require_steward_principal(steward_principal())
        with self.assertRaises(PermissionError):
            require_steward_principal(operator_principal())
        with self.assertRaises(PermissionError):
            require_steward_principal(make_executor_principal("a", "e"))


# ---------------------------------------------------------------------------
# AuthorizationManager principal enforcement + reservations
# ---------------------------------------------------------------------------


class AuthorizationPrincipalEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.manager = AuthorizationManager(settings(self.base))

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_steward_principal_can_issue_and_records_issuer(self) -> None:
        principal = steward_principal()
        grant = self.manager.issue(
            "test-project", "commit", "abc123", principal=principal
        )
        self.assertEqual(grant.detail["issuer_principal_id"], "local:steward")
        self.assertEqual(grant.detail["issuer_provenance"], "local_single_user")
        self.assertEqual(grant.detail["issuer_role"], "steward")

    def test_executor_principal_cannot_issue(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "executor"):
            self.manager.issue(
                "test-project",
                "commit",
                "abc123",
                principal=make_executor_principal("a", "e"),
            )

    def test_unauthenticated_principal_cannot_issue(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "unauthenticated"):
            self.manager.issue(
                "test-project", "commit", "abc123", principal=make_legacy_principal()
            )

    def test_executor_namespace_actor_cannot_issue(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "executor"):
            self.manager.issue(
                "test-project", "commit", "abc123", actor="executor:forged"
            )

    def test_executor_principal_cannot_check_or_consume(self) -> None:
        grant = self.manager.issue("test-project", "commit", "abc123")
        executor = make_executor_principal("a", "e")
        with self.assertRaisesRegex(AuthorizationError, "executor"):
            self.manager.check(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=executor,
            )
        with self.assertRaisesRegex(AuthorizationError, "executor"):
            self.manager.consume(grant.authorization_id, principal=executor)

    def test_issue_without_principal_still_works(self) -> None:
        grant = self.manager.issue("test-project", "commit", "abc123")
        self.assertEqual(grant.detail, {})


class GrantReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.manager = AuthorizationManager(settings(self.base))
        self.principal = steward_principal()

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def _issue(self, **kwargs: object) -> Authorization:
        return self.manager.issue(
            "test-project", "commit", "abc123", principal=self.principal, **kwargs
        )

    def test_reserve_commit_consumes_grant(self) -> None:
        grant = self._issue()
        reservation = self.manager.reserve_grant(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
            principal=self.principal,
        )
        self.assertIsInstance(reservation, GrantReservation)
        committed = self.manager.commit_grant_reservation(reservation.reservation_id)
        self.assertTrue(committed.used)
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.check(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_rollback_leaves_grant_usable(self) -> None:
        grant = self._issue()
        reservation = self.manager.reserve_grant(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
            principal=self.principal,
        )
        self.assertTrue(self.manager.rollback_grant_reservation(reservation.reservation_id))
        # Still reservable + consumable after rollback.
        reservation2 = self.manager.reserve_grant(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
            principal=self.principal,
        )
        self.manager.commit_grant_reservation(reservation2.reservation_id)

    def test_double_reserve_rejected(self) -> None:
        grant = self._issue()
        self.manager.reserve_grant(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
            principal=self.principal,
        )
        with self.assertRaisesRegex(AuthorizationError, "already reserved"):
            self.manager.reserve_grant(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=self.principal,
            )

    def test_reserve_rejects_expired_grant(self) -> None:
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        grant = self._issue(expires_at=past)
        with self.assertRaisesRegex(AuthorizationError, "expired"):
            self.manager.reserve_grant(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=self.principal,
            )

    def test_reserve_rejects_revoked_grant(self) -> None:
        grant = self._issue()
        self.manager.revoke(grant.authorization_id)
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.reserve_grant(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=self.principal,
            )

    def test_reserve_rejects_executor_principal(self) -> None:
        grant = self._issue()
        with self.assertRaisesRegex(AuthorizationError, "executor"):
            self.manager.reserve_grant(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=make_executor_principal("a", "e"),
            )

    def test_commit_unknown_reservation_rejected(self) -> None:
        with self.assertRaisesRegex(AuthorizationError, "unknown reservation_id"):
            self.manager.commit_grant_reservation("0" * 32)

    def test_rollback_unknown_is_idempotent_noop(self) -> None:
        self.assertFalse(self.manager.rollback_grant_reservation("0" * 32))

    def test_run_with_grant_reservation_commits_on_success(self) -> None:
        grant = self._issue()
        calls: list[str] = []

        def work() -> str:
            calls.append("work")
            return "done"

        result = self.manager.run_with_grant_reservation(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
            principal=self.principal,
            function=work,
        )
        self.assertEqual(result, "done")
        self.assertEqual(calls, ["work"])
        with self.assertRaisesRegex(AuthorizationError, "already been used"):
            self.manager.check(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
            )

    def test_run_with_grant_reservation_rolls_back_on_error(self) -> None:
        grant = self._issue()

        def failing() -> None:
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.manager.run_with_grant_reservation(
                grant.authorization_id,
                project_id="test-project",
                capability="commit",
                target="abc123",
                principal=self.principal,
                function=failing,
            )
        # The grant is still usable after rollback.
        self.manager.check(
            grant.authorization_id,
            project_id="test-project",
            capability="commit",
            target="abc123",
        )

    def test_reserve_rejects_wrong_target(self) -> None:
        grant = self._issue()
        with self.assertRaisesRegex(AuthorizationError, "different target"):
            self.manager.reserve_grant(
                "test-project",
                grant.authorization_id,
                target_id="wrong-target",
                principal=self.principal,
            )

    def test_reserve_rejects_wrong_scope(self) -> None:
        grant = self._issue()
        with self.assertRaisesRegex(AuthorizationError, "different capability"):
            self.manager.reserve_grant(
                "test-project",
                grant.authorization_id,
                scope="wrong-scope",
                principal=self.principal,
            )

    def test_reserve_rejects_wrong_evidence_digest(self) -> None:
        grant = self._issue(detail={"bound_evidence_digest": "sha256-expected"})
        with self.assertRaisesRegex(AuthorizationError, "evidence digest"):
            self.manager.reserve_grant(
                "test-project",
                grant.authorization_id,
                evidence_digest="sha256-wrong",
                principal=self.principal,
            )

    def test_three_arg_reserve_commit_and_rollback(self) -> None:
        grant = self._issue()
        # Reserve using project_id, grant_id
        res_id = self.manager.reserve_grant(
            "test-project",
            grant.authorization_id,
            principal=self.principal,
        )
        self.assertIsInstance(res_id, str)
        # Rollback using project_id, grant_id, res_id
        rb = self.manager.rollback_grant_reservation(
            "test-project", grant.authorization_id, str(res_id), reason="retry"
        )
        self.assertTrue(rb)
        self.assertEqual(rb["status"], "rolled_back")

        # Reserve again and commit using 3-arg style
        res_id2 = self.manager.reserve_grant(
            "test-project",
            grant.authorization_id,
            principal=self.principal,
        )
        commit_res = self.manager.commit_grant_reservation(
            "test-project",
            grant.authorization_id,
            str(res_id2),
            consumed_by_id="consumer-1",
            principal=self.principal,
        )
        self.assertEqual(commit_res["status"], "consumed")
        self.assertEqual(commit_res["consumed_by_id"], "consumer-1")
        self.assertTrue(commit_res.used)


# ---------------------------------------------------------------------------
# Review promotion governance
# ---------------------------------------------------------------------------


def project(base: Path) -> ProjectPolicy:
    return ProjectPolicy.model_validate(
        {
            "id": "test-project",
            "display_name": "Test Project",
            "description": "Promotion tests",
            "root": str(base / "repo"),
            "enabled": True,
            "require_git": True,
            "capabilities": ["repository_read"],
            "markdown_write_roots": [],
            "protected_paths": [".git", ".env"],
            "default_effort": "high",
            "allow_ultracode": True,
            "self_managed": False,
        }
    )


def approval_grant(*, detail: dict[str, object] | None = None, used: bool = False) -> Authorization:
    return Authorization(
        authorization_id="grant-1",
        project_id="test-project",
        job_id="job-1",
        actor="local:steward",
        authority_source="chatgpt_confirmation",
        capability="promote",
        target="job-1",
        issued_at=datetime.now(UTC).isoformat(),
        expires_at=None,
        single_use=True,
        used=used,
        delegated_from=None,
        detail=detail or {
            "issuer_principal_id": "local:steward",
            "issuer_provenance": "local_single_user",
            "issuer_role": "steward",
        },
    )


class ReviewDecisionTests(unittest.TestCase):
    def test_grants_mutation_authority_is_false_by_default(self) -> None:
        decision = ReviewDecision(
            review_id="r1",
            project_id="p",
            job_id="j",
            decision="approve",
        )
        self.assertFalse(decision.grants_mutation_authority)

    def test_grants_mutation_authority_true_is_rejected(self) -> None:
        with self.assertRaises(ReviewError):
            ReviewDecision(
                review_id="r1",
                project_id="p",
                job_id="j",
                decision="approve",
                grants_mutation_authority=True,
            )


class ReviewManagerPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = project(self.base)
        self.manager = ReviewManager(
            settings(self.base),
            ProjectLockManager(),
            mock.Mock(),  # jobs
            mock.Mock(),  # executor
            projects=(self.project,),
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def test_record_review_decision_binds_to_candidate(self) -> None:
        decision = self.manager.record_review_decision(
            self.project,
            "job-1",
            "approve",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=["candidate.txt"],
            principal=steward_principal(),
        )
        self.assertEqual(decision.decision, "approve")
        self.assertEqual(decision.candidate_head, "abc")
        self.assertEqual(decision.diff_fingerprint, "fp")
        self.assertEqual(decision.paths, ("candidate.txt",))
        self.assertFalse(decision.grants_mutation_authority)
        self.assertEqual(decision.reviewer_principal_id, "local:steward")

    def test_record_review_decision_rejects_unknown_decision(self) -> None:
        with self.assertRaises(ReviewError):
            self.manager.record_review_decision(
                self.project,
                "job-1",
                "maybe",
                candidate_head="abc",
                diff_fingerprint="fp",
                paths=[],
            )

    def test_record_review_decision_normalizes_aliases(self) -> None:
        for alias in ("Approved", "pass", "ACCEPT"):
            decision = self.manager.record_review_decision(
                self.project,
                "job-1",
                alias,
                candidate_head="abc",
                diff_fingerprint="fp",
                paths=[],
            )
            self.assertEqual(decision.decision, "approve")

    def test_build_approval_summary_without_grant(self) -> None:
        decision = ReviewDecision(
            review_id="r1",
            project_id="test-project",
            job_id="job-1",
            decision="approve",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=("candidate.txt",),
        )
        summary = self.manager.build_approval_summary(
            self.project, "job-1", decision
        )
        self.assertIsInstance(summary, ApprovalSummaryDTO)
        self.assertFalse(summary.promoted)
        self.assertIsNone(summary.approval_grant_id)

    def test_build_approval_summary_with_grant(self) -> None:
        decision = ReviewDecision(
            review_id="r1",
            project_id="test-project",
            job_id="job-1",
            decision="approve",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=("candidate.txt",),
        )
        summary = self.manager.build_approval_summary(
            self.project, "job-1", decision, approval_grant()
        )
        self.assertTrue(summary.promoted)
        self.assertEqual(summary.approval_grant_id, "grant-1")
        self.assertEqual(summary.approver_principal_id, "local:steward")
        self.assertEqual(summary.approver_role, "steward")

    def _verify(self, **kwargs: object) -> PromotionAuthorityTrace:
        return self.manager.verify_two_gate_promotion(
            self.project,
            "job-1",
            review_decision=kwargs["review_decision"],  # type: ignore[index]
            approval_grant=kwargs["approval_grant"],  # type: ignore[index]
            expected_head=kwargs["expected_head"],  # type: ignore[index]
            expected_diff_fingerprint=kwargs["expected_diff_fingerprint"],  # type: ignore[index]
            paths=kwargs["paths"],  # type: ignore[index]
            executor_principal=kwargs.get("executor_principal"),  # type: ignore[arg-type]
            publisher_principal=kwargs.get("publisher_principal"),  # type: ignore[arg-type]
        )

    def _approve_decision(self, reviewer_id: str = "local:reviewer") -> ReviewDecision:
        return ReviewDecision(
            review_id="r1",
            project_id="test-project",
            job_id="job-1",
            decision="approve",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=("candidate.txt",),
            reviewer_principal_id=reviewer_id,
            reviewer_provenance="local_single_user",
        )

    def test_verify_two_gate_promotion_succeeds(self) -> None:
        executor = make_executor_principal("adapter", "exec-1")
        publisher = CallerPrincipal(
            principal_id="local:publisher",
            provenance=AuthProvenance.LOCAL_SINGLE_USER,
            role=PrincipalRole.OPERATOR,
            transport_mode="stdio",
            authenticated=True,
        )
        trace = self._verify(
            review_decision=self._approve_decision(),
            approval_grant=approval_grant(),
            expected_head="abc",
            expected_diff_fingerprint="fp",
            paths=["candidate.txt"],
            executor_principal=executor,
            publisher_principal=publisher,
        )
        self.assertTrue(trace.verified)
        self.assertEqual(trace.executor_principal_id, "executor:adapter:exec-1")
        self.assertEqual(trace.reviewer_principal_id, "local:reviewer")
        self.assertEqual(trace.approver_principal_id, "local:steward")
        self.assertEqual(trace.publisher_principal_id, "local:publisher")
        self.assertEqual(
            trace.trace,
            ("gate1:review-matches-candidate", "gate2:non-executor-steward-approval", "gate2:separation-of-duties"),
        )

    def test_gate1_rejects_non_approve_decision(self) -> None:
        decision = ReviewDecision(
            review_id="r1",
            project_id="test-project",
            job_id="job-1",
            decision="reject",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=("candidate.txt",),
        )
        with self.assertRaisesRegex(ReviewError, "approving"):
            self._verify(
                review_decision=decision,
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate1_rejects_head_mismatch(self) -> None:
        with self.assertRaisesRegex(ReviewError, "base HEAD"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(),
                expected_head="different",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate1_rejects_fingerprint_mismatch(self) -> None:
        with self.assertRaisesRegex(ReviewError, "fingerprint"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="different",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate1_rejects_paths_mismatch(self) -> None:
        with self.assertRaisesRegex(ReviewError, "path"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["other.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate2_rejects_executor_approver(self) -> None:
        executor_grant = approval_grant(
            detail={
                "issuer_principal_id": "executor:a:e",
                "issuer_provenance": "executor_internal",
                "issuer_role": "executor",
            }
        )
        with self.assertRaisesRegex(ReviewError, "non-executor"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=executor_grant,
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate2_rejects_non_steward_approver(self) -> None:
        operator_grant = approval_grant(
            detail={
                "issuer_principal_id": "local:operator",
                "issuer_provenance": "local_single_user",
                "issuer_role": "operator",
            }
        )
        with self.assertRaisesRegex(ReviewError, "steward"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=operator_grant,
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate2_rejects_used_grant(self) -> None:
        with self.assertRaisesRegex(ReviewError, "consumed"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(used=True),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_gate2_rejects_collapsed_identities(self) -> None:
        # Publisher is the same principal as the reviewer.
        publisher = CallerPrincipal(
            principal_id="local:reviewer",
            provenance=AuthProvenance.LOCAL_SINGLE_USER,
            role=PrincipalRole.REVIEWER,
            transport_mode="stdio",
            authenticated=True,
        )
        with self.assertRaisesRegex(ReviewError, "distinct"):
            self._verify(
                review_decision=self._approve_decision(reviewer_id="local:reviewer"),
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=publisher,
            )

    def test_gate2_rejects_missing_principal(self) -> None:
        with self.assertRaisesRegex(ReviewError, "identified"):
            self._verify(
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=None,
            )

    def test_gate1_rejects_failed_verification(self) -> None:
        with self.assertRaisesRegex(ReviewError, "verification"):
            self.manager.verify_two_gate_promotion(
                self.project,
                "job-1",
                review_decision=self._approve_decision(),
                approval_grant=approval_grant(),
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                verification_passed=False,
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )

    def test_record_review_decision_rejects_executor_reviewer(self) -> None:
        with self.assertRaises(PermissionError):
            self.manager.record_review_decision(
                self.project,
                "job-1",
                "approve",
                candidate_head="abc",
                diff_fingerprint="fp",
                paths=["candidate.txt"],
                reviewer=make_executor_principal("a", "e"),
            )

    def test_record_review_decision_rejects_self_review(self) -> None:
        reviewer = CallerPrincipal(
            principal_id="executor:a:e",
            provenance=AuthProvenance.EXECUTOR_INTERNAL,
            role=PrincipalRole.EXECUTOR,
            authenticated=True,
        )
        with self.assertRaises(PermissionError):
            self.manager.record_review_decision(
                self.project,
                "job-1",
                "approve",
                candidate_head="abc",
                diff_fingerprint="fp",
                paths=["candidate.txt"],
                reviewer=reviewer,
                executor_principal_id="executor:a:e",
            )

    def test_record_review_decision_rejects_approval_when_verification_failed(self) -> None:
        with self.assertRaisesRegex(ReviewError, "verification"):
            self.manager.record_review_decision(
                self.project,
                "job-1",
                "approve",
                candidate_head="abc",
                diff_fingerprint="fp",
                paths=["candidate.txt"],
                verification_passed=False,
                principal=steward_principal(),
            )

    def test_gate2_rejects_wrong_evidence_digest(self) -> None:
        grant = approval_grant(
            detail={
                "issuer_principal_id": "local:steward",
                "issuer_provenance": "local_single_user",
                "issuer_role": "steward",
                "bound_evidence_digest": "sha256-wrong",
            }
        )
        decision = self.manager.record_review_decision(
            self.project,
            "job-1",
            "approve",
            candidate_head="abc",
            diff_fingerprint="fp",
            paths=["candidate.txt"],
            principal=steward_principal(),
        )
        with self.assertRaisesRegex(ReviewError, "evidence digest"):
            self.manager.verify_two_gate_promotion(
                self.project,
                "job-1",
                review_decision=decision,
                approval_grant=grant,
                expected_head="abc",
                expected_diff_fingerprint="fp",
                paths=["candidate.txt"],
                executor_principal=make_executor_principal("a", "e"),
                publisher_principal=operator_principal(),
            )


if __name__ == "__main__":
    unittest.main()
