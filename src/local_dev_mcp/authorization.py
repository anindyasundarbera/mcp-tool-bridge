"""Durable per-action authorization objects for the local development bridge.

M-GV-01: replaces inline-evidence re-statement with durable, target-specific
authorization records. An ``AuthorizationManager`` issues, checks, consumes,
and expires authorizations held in the additive ``authorizations`` table.

Internal-first: the manager is invoked from existing sensitive handlers
(commit, push, review, export), not exposed as a new MCP tool. The inline-
evidence path remains the default until a project opts in, so behavior is
unchanged for existing projects.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from . import store
from .config import Settings
from .identity import AuthProvenance, CallerPrincipal, PrincipalRole
from .worktrees import GitWorktreeManager

T = TypeVar("T")


class AuthorizationError(ValueError, RuntimeError):
    """Raised when an authorization cannot be issued, checked, or consumed."""


class AuthorizationPermissionError(AuthorizationError, PermissionError):
    """Raised when an authorization operation fails institutional authority checks."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class GrantReservation(str):
    """A soft, in-memory hold on an authorization preventing double-spend."""

    reservation_id: str
    authorization_id: str
    reserved_at: str
    scope: str | None
    target_id: str | None
    evidence_digest: str | None
    principal_id: str | None

    def __new__(
        cls,
        reservation_id: str,
        authorization_id: str,
        reserved_at: str,
        scope: str | None = None,
        target_id: str | None = None,
        evidence_digest: str | None = None,
        principal_id: str | None = None,
    ) -> GrantReservation:
        instance = super().__new__(cls, reservation_id)
        instance.reservation_id = reservation_id
        instance.authorization_id = authorization_id
        instance.reserved_at = reserved_at
        instance.scope = scope
        instance.target_id = target_id
        instance.evidence_digest = evidence_digest
        instance.principal_id = principal_id
        return instance


class CommittedGrantResult(dict[str, Any]):
    """Result of committing a grant reservation, accessible as a dict or grant object."""

    def __init__(self, audit: dict[str, Any], authorization: Authorization) -> None:
        super().__init__(audit)
        self.authorization = authorization

    @property
    def used(self) -> bool:
        return self.authorization.used

    @property
    def authorization_id(self) -> str:
        return self.authorization.authorization_id


class RollbackGrantResult(dict[str, Any]):
    """Result of rolling back a grant reservation, evaluating truthy if released."""

    def __init__(self, data: dict[str, Any], released: bool) -> None:
        super().__init__(data)
        self._released = released

    def __bool__(self) -> bool:
        return self._released


@dataclass(frozen=True, slots=True)
class Authorization:
    """One durable, target-specific authorization record."""

    authorization_id: str
    project_id: str
    job_id: str | None
    actor: str
    authority_source: str
    capability: str
    target: str
    issued_at: str
    expires_at: str | None
    single_use: bool
    used: bool
    delegated_from: str | None
    detail: dict[str, Any]


def authorization_to_row(authorization: Authorization) -> dict[str, Any]:
    return {
        "authorization_id": authorization.authorization_id,
        "project_id": authorization.project_id,
        "job_id": authorization.job_id,
        "actor": authorization.actor,
        "authority_source": authorization.authority_source,
        "capability": authorization.capability,
        "target": authorization.target,
        "issued_at": authorization.issued_at,
        "expires_at": authorization.expires_at,
        "single_use": 1 if authorization.single_use else 0,
        "used": 1 if authorization.used else 0,
        "delegated_from": authorization.delegated_from,
        "detail_json": json.dumps(authorization.detail, ensure_ascii=False),
    }


def authorization_from_row(row: dict[str, Any]) -> Authorization | None:
    try:
        detail = json.loads(row.get("detail_json") or "{}")
    except (ValueError, TypeError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    try:
        return Authorization(
            authorization_id=row["authorization_id"],
            project_id=row["project_id"],
            job_id=row.get("job_id"),
            actor=row["actor"],
            authority_source=row["authority_source"],
            capability=row["capability"],
            target=row["target"],
            issued_at=row["issued_at"],
            expires_at=row.get("expires_at"),
            single_use=bool(row.get("single_use", 1)),
            used=bool(row.get("used", 0)),
            delegated_from=row.get("delegated_from"),
            detail=detail,
        )
    except (KeyError, TypeError, ValueError):
        return None


class AuthorizationManager:
    """Issues, checks, consumes, and expires durable authorizations."""

    def __init__(
        self,
        settings: Settings,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[Any, ...] = (),
        state_db: sqlite3.Connection | None = None,
    ) -> None:
        self.settings = settings
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        if state_db is not None:
            self._db = state_db
            self._owns_db = False
        else:
            self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
            self._owns_db = True
        # M2.5: in-memory reservation state. ``reservation_id`` → reservation;
        # ``authorization_id`` → reservation_id for fast double-spend checks.
        self._lock = threading.Lock()
        self._reservations: dict[str, GrantReservation] = {}
        self._reserved_authorization_ids: dict[str, str] = {}

    def _new_authorization_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if store.load_authorization_row(self._db, candidate) is None:
                return candidate
        raise AuthorizationError("could not allocate a unique authorization ID")

    def _new_reservation_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if candidate not in self._reservations:
                return candidate
        raise AuthorizationError("could not allocate a unique reservation ID")

    @staticmethod
    def _reject_executor_principal(
        principal: CallerPrincipal | None,
        action: str,
    ) -> None:
        """Reject an executor or unauthenticated principal for an authority action.

        An ``EXECUTOR`` role, an unauthenticated principal, or any principal in
        the ``executor:`` namespace may not issue or consume a grant. Fail
        closed: the executor can never wield bridge authority.
        """
        if principal is None:
            return
        if not principal.authenticated:
            raise AuthorizationPermissionError(f"an unauthenticated principal cannot {action}")
        if principal.role is PrincipalRole.EXECUTOR or principal.provenance is AuthProvenance.EXECUTOR_INTERNAL:
            raise AuthorizationPermissionError(f"an executor principal cannot {action}")
        if principal.principal_id.startswith("executor:"):
            raise AuthorizationPermissionError(
                f"an executor-namespace principal cannot {action}"
            )

    def close(self) -> None:
        """Close the durable state database if this manager owns it."""
        if getattr(self, "_owns_db", False) and getattr(self, "_db", None) is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None  # type: ignore[assignment]

    def issue(
        self,
        project_id: str,
        capability: str,
        target: str,
        *,
        actor: str = "ChatGPT",
        authority_source: str = "chatgpt_confirmation",
        principal: CallerPrincipal | None = None,
        job_id: str | None = None,
        expires_at: str | None = None,
        single_use: bool = True,
        delegated_from: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Authorization:
        """Issue a durable authorization for a capability + target.

        M2.5: the issuer is an authenticated, non-executor principal. An
        executor, an unauthenticated principal, or any ``executor:``-namespaced
        actor/issuer is rejected, and the issuing principal's identity and
        provenance are recorded in the grant ``detail`` so a later promotion
        gate can verify separation of duties.
        """
        self._reject_executor_principal(principal, "issue authorizations")
        if actor.startswith("executor:"):
            raise AuthorizationError(
                "actor in the 'executor:' namespace cannot issue authorizations"
            )
        record_detail = dict(detail or {})
        if principal is not None:
            record_detail["issuer_principal_id"] = principal.principal_id
            record_detail["issuer_provenance"] = principal.provenance.value
            record_detail["issuer_role"] = principal.role.value
        authorization = Authorization(
            authorization_id=self._new_authorization_id(),
            project_id=project_id,
            job_id=job_id,
            actor=actor,
            authority_source=authority_source,
            capability=capability,
            target=target,
            issued_at=_timestamp(),
            expires_at=expires_at,
            single_use=single_use,
            used=False,
            delegated_from=delegated_from,
            detail=record_detail,
        )
        store.insert_authorization_row(self._db, authorization_to_row(authorization))
        return authorization

    def check(
        self,
        authorization_id: str,
        *,
        project_id: str,
        capability: str,
        target: str,
        principal: CallerPrincipal | None = None,
    ) -> Authorization:
        """Return a valid, unused, unexpired authorization, or raise.

        The authorization must match the project, capability, and target, must
        not be used, and must not be expired. Consumption (marking ``used``) is
        a separate ``consume`` call so a caller can validate before acting.

        M2.5: the caller principal must be authenticated and non-executor.
        """
        self._reject_executor_principal(principal, "check authorizations")
        row = store.load_authorization_row(self._db, authorization_id)
        if row is None:
            raise AuthorizationError("unknown authorization_id")
        authorization = authorization_from_row(row)
        if authorization is None:
            raise AuthorizationError("authorization record is corrupt")
        if authorization.project_id != project_id:
            raise AuthorizationError("authorization is for a different project")
        if authorization.capability != capability:
            raise AuthorizationError("authorization is for a different capability")
        if authorization.target != target:
            raise AuthorizationError("authorization is for a different target")
        if authorization.used:
            raise AuthorizationError("authorization has already been used")
        if authorization.expires_at is not None:
            try:
                expires = datetime.fromisoformat(authorization.expires_at)
            except ValueError as exc:
                raise AuthorizationError("authorization has an invalid expiry") from exc
            if datetime.now(UTC) > expires:
                raise AuthorizationError("authorization has expired")
        return authorization

    def consume(self, authorization_id: str, *, principal: CallerPrincipal | None = None) -> None:
        """Atomically mark an authorization as used (single-use consumption).

        Raises if the authorization was already used (the atomic ``WHERE used =
        0`` guard means only the first concurrent consumer wins).

        M2.5: the caller principal must be authenticated and non-executor.
        """
        self._reject_executor_principal(principal, "consume authorizations")
        if not store.mark_authorization_used(self._db, authorization_id):
            raise AuthorizationError("authorization has already been used")

    def check_and_consume(
        self,
        authorization_id: str,
        *,
        project_id: str,
        capability: str,
        target: str,
        principal: CallerPrincipal | None = None,
    ) -> Authorization:
        """Validate and atomically consume an authorization in one step.

        This is the TOCTOU-safe path for sensitive handlers: it validates the
        project/capability/target and unused/unexpired state, then atomically
        marks the authorization used. Only one concurrent caller can win; a
        second sees the already-used state and raises.

        M2.5: the caller principal must be authenticated and non-executor.
        """
        authorization = self.check(
            authorization_id,
            project_id=project_id,
            capability=capability,
            target=target,
            principal=principal,
        )
        self.consume(authorization_id, principal=principal)
        return authorization

    def get(self, authorization_id: str) -> Authorization | None:
        """Return an authorization record, or None if it does not exist."""
        row = store.load_authorization_row(self._db, authorization_id)
        return authorization_from_row(row) if row is not None else None

    def list_active_for(
        self,
        project_id: str,
        job_id: str | None = None,
    ) -> list[Authorization]:
        """Return a project's in-force authorizations (unused + unexpired).

        M-CC-08: the dashboard enrichment surfaces the authorizations a human
        has granted that are still available to be consumed. ``used`` is
        filtered at the store layer; expiry is filtered here with the same
        semantics as ``check`` (a ``NULL`` ``expires_at`` never expires, and a
        corrupt expiry is treated as not in force). Optionally narrowed to one
        job. Read-only — never consumes or mutates.
        """
        active: list[Authorization] = []
        now = datetime.now(UTC)
        for row in store.load_authorization_rows_for(self._db, project_id):
            authorization = authorization_from_row(row)
            if authorization is None:
                continue
            # A job-scoped query also surfaces project-scoped grants
            # (``job_id`` NULL), which apply to any job in the project.
            if (
                job_id is not None
                and authorization.job_id is not None
                and authorization.job_id != job_id
            ):
                continue
            if authorization.expires_at is not None:
                try:
                    expires = datetime.fromisoformat(authorization.expires_at)
                except ValueError:
                    continue
                if now > expires:
                    continue
            active.append(authorization)
        return active

    def revoke(self, authorization_id: str) -> Authorization | None:
        """Atomically mark an authorization as revoked (used).

        M-CC-08: the revoke_authorization human control marks a previously
        granted authorization consumed so it can no longer be used, regardless
        of whether it was job-scoped or project-scoped. Idempotent and
        repeat-safe: revoking an unknown or already-used authorization is a
        no-op returning ``None``. Returns the authorization only when it was
        newly revoked by this call.
        """
        row = store.load_authorization_row(self._db, authorization_id)
        if row is None:
            return None
        authorization = authorization_from_row(row)
        if authorization is None or authorization.used:
            return None
        if not store.mark_authorization_used(self._db, authorization_id):
            return None
        return authorization

    # ------------------------------------------------------------------
    # M2.5: reservation state machine. A reservation is a soft, in-memory hold
    # that prevents double-spend across a reserve → commit/rollback window while
    # a multi-step flow (e.g. a two-gate promotion) is in flight. The durable
    # ``used`` flag is only mutated at ``commit`` (atomically), so a crash or
    # rollback leaves the grant unconsumed and safely re-reservable.
    # ------------------------------------------------------------------

    def reserve_grant(
        self,
        *args: Any,
        project_id: str | None = None,
        grant_id: str | None = None,
        authorization_id: str | None = None,
        scope: str | None = None,
        capability: str | None = None,
        target_id: str | None = None,
        target: str | None = None,
        evidence_digest: str | None = None,
        principal: CallerPrincipal | None = None,
        **kwargs: Any,
    ) -> GrantReservation:
        """Reserve a valid grant, rejecting double-spend and executor callers.

        Validates the grant exists, belongs to ``project_id``, is not expired,
        not revoked, not already consumed, and NOT already reserved (double spend
        rejected). Validates ``scope``, ``target_id``, and ``evidence_digest``
        when supplied, and rejects executor or unauthenticated principals.
        """
        with self._lock:
            if len(args) >= 2:
                eff_project_id = str(args[0])
                eff_grant_id = str(args[1])
            elif len(args) == 1:
                eff_grant_id = str(args[0])
                eff_project_id = project_id or kwargs.get("project_id") or ""
            else:
                eff_grant_id = (
                    grant_id
                    or authorization_id
                    or kwargs.get("grant_id")
                    or kwargs.get("authorization_id")
                    or ""
                )
                eff_project_id = project_id or kwargs.get("project_id") or ""

            if not eff_grant_id or not eff_project_id:
                raise AuthorizationError("project_id and grant_id are required to reserve a grant")

            self._reject_executor_principal(principal, "reserve grants")

            grant = self.get(eff_grant_id)
            if grant is None:
                raise AuthorizationError("unknown authorization_id")
            if grant.project_id != eff_project_id:
                raise AuthorizationError("authorization is for a different project")

            eff_scope = scope or capability
            if eff_scope is not None and grant.capability != eff_scope:
                raise AuthorizationError("authorization is for a different capability")

            eff_target = target_id or target
            if eff_target is not None and grant.target != eff_target:
                raise AuthorizationError("authorization is for a different target")

            if evidence_digest is not None:
                bound_digest = grant.detail.get("evidence_digest") or grant.detail.get(
                    "bound_evidence_digest"
                )
                if bound_digest and bound_digest != evidence_digest:
                    raise AuthorizationError(
                        "authorization is bound to a different evidence digest"
                    )

            if grant.used:
                raise AuthorizationError("authorization has already been used")

            if grant.expires_at is not None:
                try:
                    expires = datetime.fromisoformat(grant.expires_at)
                except ValueError as exc:
                    raise AuthorizationError("authorization has an invalid expiry") from exc
                if datetime.now(UTC) > expires:
                    raise AuthorizationError("authorization has expired")

            if eff_grant_id in self._reserved_authorization_ids:
                raise AuthorizationError("authorization is already reserved")

            reservation = GrantReservation(
                reservation_id=self._new_reservation_id(),
                authorization_id=eff_grant_id,
                reserved_at=_timestamp(),
                scope=eff_scope,
                target_id=eff_target,
                evidence_digest=evidence_digest,
                principal_id=principal.principal_id if principal else None,
            )
            self._reservations[reservation.reservation_id] = reservation
            self._reserved_authorization_ids[eff_grant_id] = reservation.reservation_id
            return reservation

    def commit_grant_reservation(
        self,
        *args: Any,
        reservation_id: str | None = None,
        project_id: str | None = None,
        grant_id: str | None = None,
        consumed_by_id: str | None = None,
        principal: CallerPrincipal | None = None,
        **kwargs: Any,
    ) -> Any:
        """Atomically commit an active reservation by consuming its grant."""
        with self._lock:
            call_style = "one_arg"
            proj_id: str | None = None
            g_id: str | None = None
            if len(args) == 3:
                proj_id = str(args[0])
                g_id = str(args[1])
                res_id = str(args[2])
                call_style = "three_args"
            elif len(args) == 1:
                res_id = str(args[0])
                proj_id = str(project_id or kwargs.get("project_id")) if (project_id or kwargs.get("project_id")) else None
                g_id = str(grant_id or kwargs.get("grant_id")) if (grant_id or kwargs.get("grant_id")) else None
            else:
                res_id = str(reservation_id or kwargs.get("reservation_id") or "")
                proj_id = str(project_id or kwargs.get("project_id")) if (project_id or kwargs.get("project_id")) else None
                g_id = str(grant_id or kwargs.get("grant_id")) if (grant_id or kwargs.get("grant_id")) else None
                if "consumed_by_id" in kwargs or "principal" in kwargs:
                    call_style = "three_args"

            if not res_id:
                raise AuthorizationError("reservation_id is required")

            self._reject_executor_principal(principal, "commit grant reservations")

            reservation = self._reservations.get(res_id)
            if reservation is None:
                raise AuthorizationError(f"unknown reservation_id: {res_id}")
            if g_id is not None and reservation.authorization_id != g_id:
                raise AuthorizationError("reservation is for a different grant")

            auth_id = reservation.authorization_id
            auth_check = self.get(auth_id)
            if auth_check is not None and proj_id is not None and auth_check.project_id != proj_id:
                raise AuthorizationError("reservation is for a different project")
            self.consume(auth_id, principal=principal)
            self._release_reservation(reservation)

            auth = self.get(auth_id)
            assert auth is not None

            audit = {
                "grant_id": auth_id,
                "reservation_id": res_id,
                "project_id": auth.project_id,
                "status": "consumed",
                "consumed_by_id": consumed_by_id,
                "principal_id": principal.principal_id if principal else None,
                "provenance": principal.provenance.value if principal else None,
            }

            if call_style == "three_args":
                return CommittedGrantResult(audit, auth)
            return auth

    def rollback_grant_reservation(
        self,
        *args: Any,
        reservation_id: str | None = None,
        project_id: str | None = None,
        grant_id: str | None = None,
        reason: str = "operation_failed",
        **kwargs: Any,
    ) -> Any:
        """Release a reservation without consuming its grant."""
        with self._lock:
            proj_id: str | None = None
            g_id: str | None = None
            if len(args) == 3:
                proj_id = str(args[0])
                g_id = str(args[1])
                res_id = str(args[2])
            elif len(args) == 1:
                res_id = str(args[0])
                proj_id = str(project_id or kwargs.get("project_id")) if (project_id or kwargs.get("project_id")) else None
                g_id = str(grant_id or kwargs.get("grant_id")) if (grant_id or kwargs.get("grant_id")) else None
            else:
                res_id = str(reservation_id or kwargs.get("reservation_id") or "")
                proj_id = str(project_id or kwargs.get("project_id")) if (project_id or kwargs.get("project_id")) else None
                g_id = str(grant_id or kwargs.get("grant_id")) if (grant_id or kwargs.get("grant_id")) else None

            if not res_id:
                return RollbackGrantResult(
                    {"status": "not_found", "reason": reason}, False
                )

            reservation = self._reservations.get(res_id)
            if reservation is None:
                return RollbackGrantResult(
                    {"reservation_id": res_id, "status": "not_found", "reason": reason},
                    False,
                )

            if g_id is not None and reservation.authorization_id != g_id:
                raise AuthorizationError("reservation is for a different grant")

            auth_id = reservation.authorization_id
            self._release_reservation(reservation)
            return RollbackGrantResult(
                {
                    "grant_id": auth_id,
                    "reservation_id": res_id,
                    "project_id": proj_id or "",
                    "status": "rolled_back",
                    "reason": reason,
                },
                True,
            )

    def _release_reservation(self, reservation: GrantReservation) -> None:
        self._reservations.pop(reservation.reservation_id, None)
        if (
            self._reserved_authorization_ids.get(reservation.authorization_id)
            == reservation.reservation_id
        ):
            self._reserved_authorization_ids.pop(reservation.authorization_id, None)

    def run_with_grant_reservation(
        self,
        *args: Any,
        project_id: str | None = None,
        grant_id: str | None = None,
        authorization_id: str | None = None,
        operation: Callable[[], T] | None = None,
        function: Callable[[], T] | None = None,
        scope: str | None = None,
        capability: str | None = None,
        target_id: str | None = None,
        target: str | None = None,
        evidence_digest: str | None = None,
        principal: CallerPrincipal | None = None,
        consumed_by_id: str | None = None,
        **kwargs: Any,
    ) -> T:
        """Reserve a grant, run operation, commit on success, rollback on error."""
        if len(args) == 3:
            p_id = str(args[0])
            g_id = str(args[1])
            op = args[2]
        elif len(args) == 1:
            g_id = str(args[0])
            p_id = project_id or kwargs.get("project_id") or ""
            op = operation or function or kwargs.get("operation") or kwargs.get("function")
        else:
            p_id = project_id or kwargs.get("project_id") or ""
            g_id = (
                grant_id
                or authorization_id
                or kwargs.get("grant_id")
                or kwargs.get("authorization_id")
                or ""
            )
            op = operation or function or kwargs.get("operation") or kwargs.get("function")

        if op is None or not callable(op):
            raise AuthorizationError("an operation or function callable is required")
        if not p_id or not g_id:
            raise AuthorizationError("project_id and grant_id are required")

        eff_scope = scope or capability
        eff_target = target_id or target

        reservation = self.reserve_grant(
            p_id,
            g_id,
            scope=eff_scope,
            target_id=eff_target,
            evidence_digest=evidence_digest,
            principal=principal,
        )
        res_id = str(reservation)
        try:
            result = op()
        except BaseException:
            self.rollback_grant_reservation(
                p_id, g_id, res_id, reason="operation_failed"
            )
            raise
        self.commit_grant_reservation(
            p_id, g_id, res_id, consumed_by_id=consumed_by_id, principal=principal
        )
        return result
