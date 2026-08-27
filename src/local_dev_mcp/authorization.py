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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import store
from .config import Settings
from .worktrees import GitWorktreeManager


class AuthorizationError(RuntimeError):
    """Raised when an authorization cannot be issued, checked, or consumed."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> None:
        self.settings = settings
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")

    def _new_authorization_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if store.load_authorization_row(self._db, candidate) is None:
                return candidate
        raise AuthorizationError("could not allocate a unique authorization ID")

    def issue(
        self,
        project_id: str,
        capability: str,
        target: str,
        *,
        actor: str = "ChatGPT",
        authority_source: str = "chatgpt_confirmation",
        job_id: str | None = None,
        expires_at: str | None = None,
        single_use: bool = True,
        delegated_from: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Authorization:
        """Issue a durable authorization for a capability + target."""
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
            detail=detail or {},
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
    ) -> Authorization:
        """Return a valid, unused, unexpired authorization, or raise.

        The authorization must match the project, capability, and target, must
        not be used, and must not be expired. Consumption (marking ``used``) is
        a separate ``consume`` call so a caller can validate before acting.
        """
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

    def consume(self, authorization_id: str) -> None:
        """Atomically mark an authorization as used (single-use consumption).

        Raises if the authorization was already used (the atomic ``WHERE used =
        0`` guard means only the first concurrent consumer wins).
        """
        if not store.mark_authorization_used(self._db, authorization_id):
            raise AuthorizationError("authorization has already been used")

    def check_and_consume(
        self,
        authorization_id: str,
        *,
        project_id: str,
        capability: str,
        target: str,
    ) -> Authorization:
        """Validate and atomically consume an authorization in one step.

        This is the TOCTOU-safe path for sensitive handlers: it validates the
        project/capability/target and unused/unexpired state, then atomically
        marks the authorization used. Only one concurrent caller can win; a
        second sees the already-used state and raises.
        """
        authorization = self.check(
            authorization_id,
            project_id=project_id,
            capability=capability,
            target=target,
        )
        self.consume(authorization_id)
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
