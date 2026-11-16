"""Transport-neutral peer metadata and transport adapters for the local bridge.

M2.4: the bridge has three distinct transport surfaces — the in-process stdio
channel, a loopback-only HTTP listener, and an ngrok mTLS-gated public tunnel.
This module models those surfaces without importing FastMCP, so identity and
authorization decisions can be made against transport evidence (``mode``,
``remote_addr``, mTLS subject/verification) rather than against whatever a
caller *claims* in an HTTP header or a request body.

Every adapter is fail-closed:

* ``StdioTransportAdapter`` is local-only and never exposes a listening socket.
* ``LocalHttpTransportAdapter`` binds loopback-only and rejects any non-loopback
  remote address or untrusted ``Origin`` header.
* ``NgrokMtlsTransportAdapter`` requires both ngrok and mTLS, scrubs the
  authtoken and any certificate material from every diagnostic it emits.

The ``TransportPort`` protocol is the seam the bridge consumes, so a live
process, a mocked subprocess, or a pure in-process adapter are interchangeable
at the boundary without widening any caller's authority.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol


class TransportMode(str, Enum):
    """The three supported transport surfaces, ordered from most- to
    least-trusted (a peer identity is only cryptographically verifiable over
    ``NGROK_MTLS``)."""

    STDIO = "stdio"
    LOCAL_HTTP = "http"
    NGROK_MTLS = "ngrok_mtls"


#: Hosts the loopback HTTP transport may bind to. Anything else exposes the
#: bridge on a non-loopback interface and is rejected at config time.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_loopback_addr(addr: str) -> bool:
    """Return True when ``addr`` is a loopback address (IPv4 127/8 or ``::1``)."""
    return addr in _LOOPBACK_HOSTS or addr.startswith("127.")


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    """Static, mode-derived capabilities of a transport surface."""

    mode: TransportMode
    is_local_only: bool
    requires_mtls: bool
    requires_ngrok: bool
    supports_peer_cert_identity: bool
    bind_host: str | None = None
    bind_port: int | None = None


@dataclass(frozen=True, slots=True)
class PeerTransportMetadata:
    """Transport evidence for one peer, frozen and header-safe.

    ``untrusted_headers`` is the set of headers the transport *cannot* vouch
    for (identity-claiming headers such as ``X-Forwarded-For``,
    ``X-Client-Cert-SHA256``, ``X-SSL-Client-DN``, ``Authorization`` …). It is
    recorded (wrapped in a read-only ``MappingProxyType``) so downstream identity
    resolution can see them — and deliberately ignore them — but they can never
    be trusted as peer identity. Identity resolution never reads these back as
    authority.
    """

    transport_mode: TransportMode
    remote_addr: str | None = None
    mtls_subject_dn: str | None = None
    mtls_cert_sha256: str | None = None
    mtls_verified: bool = False
    untrusted_headers: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        wrapped: Mapping[str, str]
        headers = self.untrusted_headers
        if isinstance(headers, MappingProxyType):
            wrapped = headers
        else:
            wrapped = MappingProxyType(dict(headers or {}))
        object.__setattr__(self, "untrusted_headers", wrapped)


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Validated configuration for one transport surface.

    Fail-closed validation: ``LOCAL_HTTP`` only ever binds a loopback host, ports
    are range-checked, and the credentials of the higher-trust surfaces (ngrok
    authtoken, mTLS CA certificate) are rejected on the surfaces that must never
    carry them. ``NGROK_MTLS`` requires a real PEM CA certificate on disk.
    """

    mode: TransportMode
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1", "http://localhost")
    ngrok_domain: str | None = None
    ngrok_authtoken: str | None = None
    mtls_ca_cert_path: Path | None = None

    def __post_init__(self) -> None:
        if self.port < 0 or self.port > 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.mode is TransportMode.LOCAL_HTTP and not _is_loopback_addr(self.host):
            raise ValueError(
                "LOCAL_HTTP requires a loopback host ('127.0.0.1', "
                "'localhost', or '::1')"
            )
        if self.mode in (TransportMode.STDIO, TransportMode.LOCAL_HTTP):
            if self.ngrok_authtoken is not None:
                raise ValueError(
                    "ngrok_authtoken is not allowed for STDIO or LOCAL_HTTP"
                )
            if self.mtls_ca_cert_path is not None:
                raise ValueError(
                    "mtls_ca_cert_path is not allowed for STDIO or LOCAL_HTTP"
                )
        if self.mode is TransportMode.NGROK_MTLS:
            if self.mtls_ca_cert_path is None:
                raise ValueError("NGROK_MTLS requires an mtls_ca_cert_path")
            if not self.mtls_ca_cert_path.is_file():
                raise ValueError("mtls_ca_cert_path must exist as a file")
            try:
                pem = self.mtls_ca_cert_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError("mtls_ca_cert_path could not be read") from exc
            if "-----BEGIN CERTIFICATE-----" not in pem:
                raise ValueError(
                    "mtls_ca_cert_path must contain a PEM certificate"
                )

    def redacted_diagnostics(self) -> dict[str, Any]:
        """Safe diagnostics: the authtoken is redacted and certificate content
        is never read or exposed (only the configured path, if any)."""
        return {
            "mode": self.mode.value,
            "host": self.host,
            "port": self.port,
            "allowed_origins": list(self.allowed_origins),
            "ngrok_domain": self.ngrok_domain,
            "ngrok_authtoken": "<redacted>" if self.ngrok_authtoken else None,
            "mtls_ca_cert_path": (
                str(self.mtls_ca_cert_path) if self.mtls_ca_cert_path else None
            ),
        }


class TransportPort(Protocol):
    """The transport seam the bridge consumes."""

    def capabilities(self) -> TransportCapabilities: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]: ...

    def build_peer_metadata(
        self,
        *,
        remote_addr: str | None = None,
        mtls_subject_dn: str | None = None,
        mtls_cert_sha256: str | None = None,
        mtls_verified: bool = False,
        untrusted_headers: Mapping[str, str] | None = None,
    ) -> PeerTransportMetadata: ...


def _open_listener(host: str, port: int) -> socket.socket:
    """Bind and listen a socket on ``host:port``, raising on any collision."""
    last_exc: OSError | None = None
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(5)
            return sock
        except OSError as exc:
            sock.close()
            last_exc = exc
    raise RuntimeError(f"could not bind {host}:{port}: {last_exc}") from last_exc


class StdioTransportAdapter:
    """In-process stdio transport: local-only, never listens, always healthy."""

    def __init__(self, config: TransportConfig | None = None) -> None:
        self._config = config or TransportConfig(mode=TransportMode.STDIO)

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            mode=TransportMode.STDIO,
            is_local_only=True,
            requires_mtls=False,
            requires_ngrok=False,
            supports_peer_cert_identity=False,
            bind_host=None,
            bind_port=None,
        )

    def start(self) -> None:
        # No listener socket; stdio is always available.
        return None

    def stop(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "mode": TransportMode.STDIO.value,
            "listening": False,
        }

    def build_peer_metadata(
        self,
        *,
        remote_addr: str | None = None,
        mtls_subject_dn: str | None = None,
        mtls_cert_sha256: str | None = None,
        mtls_verified: bool = False,
        untrusted_headers: Mapping[str, str] | None = None,
    ) -> PeerTransportMetadata:
        return PeerTransportMetadata(
            transport_mode=TransportMode.STDIO,
            remote_addr=None,
            untrusted_headers=untrusted_headers or {},
        )


#: Identity-claiming headers the loopback HTTP transport can never vouch for.
#: They are recorded (never trusted) in ``PeerTransportMetadata.untrusted_headers``
#: so identity resolution has no path to mistake them for peer identity.
_UNTRUSTED_IDENTITY_HEADERS = frozenset(
    {
        "x-forwarded-for",
        "x-forwarded-host",
        "x-real-ip",
        "x-client-cert",
        "x-client-cert-sha256",
        "x-ssl-client-dn",
        "x-ssl-client-verify",
        "x-remote-user",
        "authorization",
        "proxy-authorization",
    }
)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class LocalHttpTransportAdapter:
    """Loopback-only HTTP transport with explicit bind-collision checks.

    ``start`` actually binds a listening socket (failing on collision) so the
    adapter's health reflects a real, bounded listener. ``build_peer_metadata``
    rejects any non-loopback remote address and any untrusted ``Origin`` header,
    and records identity-claiming headers as untrusted rather than honoring them.
    """

    def __init__(self, config: TransportConfig) -> None:
        if config.mode is not TransportMode.LOCAL_HTTP:
            raise ValueError("LocalHttpTransportAdapter requires LOCAL_HTTP mode")
        self._config = config
        self._socket: socket.socket | None = None

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            mode=TransportMode.LOCAL_HTTP,
            is_local_only=True,
            requires_mtls=False,
            requires_ngrok=False,
            supports_peer_cert_identity=False,
            bind_host=self._config.host,
            bind_port=self._config.port,
        )

    def probe_bind_available(self) -> bool:
        """Return True when ``host:port`` is currently bindable (no collision)."""
        try:
            sock = _open_listener(self._config.host, self._config.port)
        except RuntimeError:
            return False
        sock.close()
        return True

    def start(self) -> None:
        """Bind the listener, raising on any bind collision."""
        if self._socket is not None:
            return
        self._socket = _open_listener(self._config.host, self._config.port)

    def stop(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def health(self) -> dict[str, Any]:
        bound = self._socket is not None
        return {
            "status": "healthy" if bound else "stopped",
            "mode": TransportMode.LOCAL_HTTP.value,
            "bound": bound,
            "host": self._config.host,
            "port": self._config.port,
        }

    def is_trusted_origin(self, origin: str | None) -> bool:
        """Return True when an ``Origin`` header is in the allowed set.

        A missing origin is trusted only in the sense that it carries no claim;
        callers that require an origin must check for its absence separately.
        """
        if origin is None:
            return True
        return origin in self._config.allowed_origins

    def build_peer_metadata(
        self,
        *,
        remote_addr: str | None = None,
        mtls_subject_dn: str | None = None,
        mtls_cert_sha256: str | None = None,
        mtls_verified: bool = False,
        untrusted_headers: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> PeerTransportMetadata:
        if remote_addr is not None and not _is_loopback_addr(remote_addr):
            raise ValueError(
                "non-loopback remote_addr is not allowed over the loopback HTTP transport"
            )
        raw_headers = dict(headers or {})
        origin = _header_value(raw_headers, "origin")
        if origin is not None and not self.is_trusted_origin(origin):
            raise ValueError("untrusted origin is not allowed")
        untrusted = dict(untrusted_headers or {})
        for key, value in raw_headers.items():
            if key.lower() in _UNTRUSTED_IDENTITY_HEADERS:
                untrusted[key] = value
        return PeerTransportMetadata(
            transport_mode=TransportMode.LOCAL_HTTP,
            remote_addr=remote_addr,
            untrusted_headers=untrusted,
        )


class NgrokMtlsTransportAdapter:
    """ngrok + mTLS transport: high-trust, credential-scrubbing, hook-driven.

    Lifecycle (``start``/``stop``/``health``) is driven through an optional
    injected ``process`` (a real subprocess handle or a test double exposing
    ``start``/``stop``/``poll``), so a live ngrok tunnel and a mock are
    interchangeable without the adapter leaking its authtoken or CA material.
    """

    def __init__(self, config: TransportConfig, *, process: Any = None) -> None:
        if config.mode is not TransportMode.NGROK_MTLS:
            raise ValueError("NgrokMtlsTransportAdapter requires NGROK_MTLS mode")
        self._config = config
        self._process = process
        self._started = False

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            mode=TransportMode.NGROK_MTLS,
            is_local_only=False,
            requires_mtls=True,
            requires_ngrok=True,
            supports_peer_cert_identity=True,
            bind_host=self._config.host,
            bind_port=self._config.port,
        )

    def start(self) -> None:
        if self._started:
            return
        if self._process is not None:
            start = getattr(self._process, "start", None)
            if callable(start):
                start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._process is not None:
            stop = getattr(self._process, "stop", None)
            if callable(stop):
                stop()
        self._started = False

    def health(self) -> dict[str, Any]:
        healthy = self._started
        if self._process is not None:
            poll = getattr(self._process, "poll", None)
            if callable(poll):
                healthy = healthy and poll() is None
        # Scrub: never expose the authtoken or any certificate material.
        return {
            "status": "healthy" if healthy else "stopped",
            "mode": TransportMode.NGROK_MTLS.value,
            "domain": self._config.ngrok_domain,
            "mtls_enforced": True,
            "authtoken": "<redacted>" if self._config.ngrok_authtoken else None,
        }

    def redacted_diagnostics(self) -> dict[str, Any]:
        return self._config.redacted_diagnostics()

    def build_peer_metadata(
        self,
        *,
        remote_addr: str | None = None,
        mtls_subject_dn: str | None = None,
        mtls_cert_sha256: str | None = None,
        mtls_verified: bool = False,
        untrusted_headers: Mapping[str, str] | None = None,
    ) -> PeerTransportMetadata:
        return PeerTransportMetadata(
            transport_mode=TransportMode.NGROK_MTLS,
            remote_addr=remote_addr,
            mtls_subject_dn=mtls_subject_dn,
            mtls_cert_sha256=mtls_cert_sha256,
            mtls_verified=mtls_verified,
            untrusted_headers=untrusted_headers or {},
        )
