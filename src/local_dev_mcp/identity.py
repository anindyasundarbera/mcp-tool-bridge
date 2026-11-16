"""Caller identity resolution and principal helpers for the local bridge.

M2.5: the bridge derives a caller's identity from *transport evidence*, never
from what the caller asserts. The only identity sources are:

* ``STDIO`` / loopback ``LOCAL_HTTP`` → the trusted local single user (a
  ``STEWARD``), because only processes already on the trusted host can reach
  those surfaces.
* ``NGROK_MTLS`` → a verified mTLS peer (an ``OPERATOR``), identified by its
  certificate subject DN and fingerprint, and only when the transport reports
  ``mtls_verified`` with both a subject and a fingerprint present.
* ``EXECUTOR_INTERNAL`` → a synthetic principal minted for a contained executor
  (an ``EXECUTOR``) that can never authenticate as a steward.
* ``LEGACY_UNAUTHENTICATED`` → an ``ANONYMOUS`` principal for back-compat, only
  when the caller explicitly opts in via ``allow_legacy_unauthenticated``.

``claimed_actor_label`` and any header carried in
``PeerTransportMetadata.untrusted_headers`` are never trusted: they are recorded
at most as an unauthenticated label and are never used to establish authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .transports import (
    PeerTransportMetadata,
    TransportMode,
    _is_loopback_addr,
)


class AuthProvenance(str, Enum):
    """How a principal's identity was established (from strongest to weakest)."""

    LOCAL_SINGLE_USER = "local_single_user"
    MTLS_VERIFIED = "mtls"
    LEGACY_UNAUTHENTICATED = "legacy_unauthenticated"
    EXECUTOR_INTERNAL = "executor_internal"


class PrincipalRole(str, Enum):
    """The institutional role a principal may act in."""

    STEWARD = "steward"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
    EXECUTOR = "executor"
    ANONYMOUS = "anonymous"


@dataclass(frozen=True, slots=True)
class CallerPrincipal:
    """An authenticated (or explicitly unauthenticated) caller principal.

    Invariants enforced at construction:

    * ``principal_id`` is non-empty.
    * an ``ANONYMOUS`` role or ``LEGACY_UNAUTHENTICATED`` provenance is never
      ``authenticated``.
    * an ``EXECUTOR`` role requires ``EXECUTOR_INTERNAL`` provenance (an executor
      can never be authenticated as a steward/operator).
    """

    principal_id: str
    provenance: AuthProvenance
    role: PrincipalRole
    transport_mode: str = "stdio"
    claimed_actor_label: str | None = None
    cert_fingerprint: str | None = None
    authenticated: bool = False

    def __post_init__(self) -> None:
        if not self.principal_id or not self.principal_id.strip():
            raise ValueError("principal_id must be non-empty")
        if self.role is PrincipalRole.ANONYMOUS and self.authenticated:
            raise ValueError("an ANONYMOUS principal must not be authenticated")
        if (
            self.provenance is AuthProvenance.LEGACY_UNAUTHENTICATED
            and self.authenticated
        ):
            raise ValueError(
                "a LEGACY_UNAUTHENTICATED principal must not be authenticated"
            )
        if (
            self.role is PrincipalRole.EXECUTOR
            and self.provenance is not AuthProvenance.EXECUTOR_INTERNAL
        ):
            raise ValueError(
                "an EXECUTOR principal requires EXECUTOR_INTERNAL provenance"
            )


def make_executor_principal(adapter_id: str, execution_id: str) -> CallerPrincipal:
    """Mint a contained-executor principal (never a steward/operator)."""
    return CallerPrincipal(
        principal_id=f"executor:{adapter_id}:{execution_id}",
        provenance=AuthProvenance.EXECUTOR_INTERNAL,
        role=PrincipalRole.EXECUTOR,
        transport_mode="stdio",
        authenticated=True,
    )


def make_legacy_principal(claimed_label: str | None = None) -> CallerPrincipal:
    """Return the unauthenticated legacy principal (``ANONYMOUS``)."""
    return CallerPrincipal(
        principal_id="legacy:unauthenticated",
        provenance=AuthProvenance.LEGACY_UNAUTHENTICATED,
        role=PrincipalRole.ANONYMOUS,
        transport_mode="stdio",
        claimed_actor_label=claimed_label,
        authenticated=False,
    )


def resolve_caller_principal(
    peer: PeerTransportMetadata | None,
    *,
    claimed_actor_label: str | None = None,
    allow_legacy_unauthenticated: bool = False,
) -> CallerPrincipal:
    """Resolve a caller principal strictly from transport evidence.

    ``claimed_actor_label`` and ``peer.untrusted_headers`` are never trusted as
    identity: they are surfaced at most as the unauthenticated legacy label and
    are otherwise ignored. An absent or invalid peer fails closed
    (``PermissionError``) unless ``allow_legacy_unauthenticated`` opts into the
    anonymous legacy principal.
    """

    def _legacy() -> CallerPrincipal:
        return make_legacy_principal(claimed_actor_label)

    def _reject() -> CallerPrincipal:
        if allow_legacy_unauthenticated:
            return _legacy()
        raise PermissionError("no authenticated caller identity is available")

    if peer is None:
        return _reject()

    if peer.transport_mode is TransportMode.STDIO:
        return CallerPrincipal(
            principal_id="local:steward",
            provenance=AuthProvenance.LOCAL_SINGLE_USER,
            role=PrincipalRole.STEWARD,
            transport_mode="stdio",
            claimed_actor_label=claimed_actor_label,
            authenticated=True,
        )

    if peer.transport_mode is TransportMode.LOCAL_HTTP:
        if peer.remote_addr is not None and not _is_loopback_addr(peer.remote_addr):
            return _reject()
        return CallerPrincipal(
            principal_id="local:steward",
            provenance=AuthProvenance.LOCAL_SINGLE_USER,
            role=PrincipalRole.STEWARD,
            transport_mode="http",
            claimed_actor_label=claimed_actor_label,
            authenticated=True,
        )

    if peer.transport_mode is TransportMode.NGROK_MTLS:
        if (
            not peer.mtls_verified
            or not peer.mtls_subject_dn
            or not peer.mtls_cert_sha256
        ):
            return _reject()
        return CallerPrincipal(
            principal_id=f"mtls:{peer.mtls_subject_dn}",
            provenance=AuthProvenance.MTLS_VERIFIED,
            role=PrincipalRole.OPERATOR,
            transport_mode="ngrok_mtls",
            claimed_actor_label=claimed_actor_label,
            cert_fingerprint=peer.mtls_cert_sha256,
            authenticated=True,
        )

    return _reject()


def require_authenticated_principal(principal: CallerPrincipal | None) -> CallerPrincipal:
    """Return ``principal`` if authenticated, else raise ``PermissionError``."""
    if principal is None or not principal.authenticated:
        raise PermissionError("an authenticated principal is required")
    return principal


def require_non_executor_principal(
    principal: CallerPrincipal | None,
    action: str = "authority_action",
) -> CallerPrincipal:
    """Require an authenticated, non-executor principal for an authority action."""
    require_authenticated_principal(principal)
    assert principal is not None
    if principal.role is PrincipalRole.EXECUTOR:
        raise PermissionError(
            f"an executor principal cannot perform the authority action: {action}"
        )
    return principal


def require_steward_principal(
    principal: CallerPrincipal | None,
    action: str = "steward_action",
) -> CallerPrincipal:
    """Require an authenticated STEWARD principal for a steward action."""
    require_authenticated_principal(principal)
    assert principal is not None
    if principal.role is not PrincipalRole.STEWARD:
        raise PermissionError(
            f"only a steward principal can perform the action: {action}"
        )
    return principal


__all__ = [
    "AuthProvenance",
    "CallerPrincipal",
    "PrincipalRole",
    "make_executor_principal",
    "make_legacy_principal",
    "require_authenticated_principal",
    "require_non_executor_principal",
    "require_steward_principal",
    "resolve_caller_principal",
]
