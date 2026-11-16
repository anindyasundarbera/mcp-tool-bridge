"""M2.4: transport surfaces and peer metadata.

Covers the transport model (``TransportMode``, ``TransportCapabilities``,
``PeerTransportMetadata``, ``TransportConfig``) and the three adapters
(``StdioTransportAdapter``, ``LocalHttpTransportAdapter``,
``NgrokMtlsTransportAdapter``) — with a focus on fail-closed validation:
loopback-only binding, bind-collision detection, untrusted-origin rejection,
identity-header non-trust, and credential/token scrubbing. No network; the
ngrok adapter is exercised through a mock process.
"""

import socket
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from local_dev_mcp.transports import (
    LocalHttpTransportAdapter,
    NgrokMtlsTransportAdapter,
    PeerTransportMetadata,
    StdioTransportAdapter,
    TransportCapabilities,
    TransportConfig,
    TransportMode,
)

_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIDXTCCAkWgAwIBAgIJALm0zEXAMPLEEXAMPLEEXAMPLE\n"
    "-----END CERTIFICATE-----\n"
)


def pem_path(base: Path, name: str = "ca.pem") -> Path:
    path = base / name
    path.write_text(_PEM, encoding="utf-8")
    return path


class TransportModeTests(unittest.TestCase):
    def test_enum_values(self) -> None:
        self.assertEqual(TransportMode.STDIO.value, "stdio")
        self.assertEqual(TransportMode.LOCAL_HTTP.value, "http")
        self.assertEqual(TransportMode.NGROK_MTLS.value, "ngrok_mtls")


class TransportCapabilitiesTests(unittest.TestCase):
    def test_stdio_capabilities(self) -> None:
        caps = TransportCapabilities(
            mode=TransportMode.STDIO,
            is_local_only=True,
            requires_mtls=False,
            requires_ngrok=False,
            supports_peer_cert_identity=False,
        )
        self.assertTrue(caps.is_local_only)
        self.assertFalse(caps.requires_mtls)
        self.assertFalse(caps.requires_ngrok)
        self.assertFalse(caps.supports_peer_cert_identity)
        self.assertIsNone(caps.bind_host)
        self.assertIsNone(caps.bind_port)

    def test_ngrok_capabilities(self) -> None:
        caps = TransportCapabilities(
            mode=TransportMode.NGROK_MTLS,
            is_local_only=False,
            requires_mtls=True,
            requires_ngrok=True,
            supports_peer_cert_identity=True,
            bind_host="127.0.0.1",
            bind_port=8000,
        )
        self.assertFalse(caps.is_local_only)
        self.assertTrue(caps.requires_mtls)
        self.assertTrue(caps.requires_ngrok)
        self.assertTrue(caps.supports_peer_cert_identity)


class PeerTransportMetadataTests(unittest.TestCase):
    def test_untrusted_headers_wrapped_in_mapping_proxy(self) -> None:
        metadata = PeerTransportMetadata(
            transport_mode=TransportMode.LOCAL_HTTP,
            untrusted_headers={"x-forwarded-for": "1.2.3.4"},
        )
        self.assertIsInstance(metadata.untrusted_headers, MappingProxyType)
        self.assertEqual(metadata.untrusted_headers["x-forwarded-for"], "1.2.3.4")
        with self.assertRaises(TypeError):
            metadata.untrusted_headers["x-forwarded-for"] = "spoof"  # type: ignore[index]

    def test_default_untrusted_headers_is_empty_mapping_proxy(self) -> None:
        metadata = PeerTransportMetadata(transport_mode=TransportMode.STDIO)
        self.assertIsInstance(metadata.untrusted_headers, MappingProxyType)
        self.assertEqual(dict(metadata.untrusted_headers), {})


class TransportConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_http_config_is_valid(self) -> None:
        config = TransportConfig(mode=TransportMode.LOCAL_HTTP)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)

    def test_local_http_rejects_non_loopback_host(self) -> None:
        for host in ("0.0.0.0", "192.168.1.5", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                TransportConfig(mode=TransportMode.LOCAL_HTTP, host=host)

    def test_local_http_accepts_loopback_hosts(self) -> None:
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                config = TransportConfig(mode=TransportMode.LOCAL_HTTP, host=host)
                self.assertEqual(config.host, host)

    def test_port_range_validation(self) -> None:
        TransportConfig(mode=TransportMode.LOCAL_HTTP, port=0)
        TransportConfig(mode=TransportMode.LOCAL_HTTP, port=65535)
        for port in (-1, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                TransportConfig(mode=TransportMode.LOCAL_HTTP, port=port)

    def test_stdio_rejects_ngrok_authtoken(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.STDIO, ngrok_authtoken="secret")

    def test_stdio_rejects_mtls_ca_cert_path(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.STDIO, mtls_ca_cert_path=self.base / "ca.pem")

    def test_local_http_rejects_ngrok_authtoken(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.LOCAL_HTTP, ngrok_authtoken="secret")

    def test_local_http_rejects_mtls_ca_cert_path(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.LOCAL_HTTP, mtls_ca_cert_path=self.base / "ca.pem")

    def test_ngrok_mtls_requires_ca_cert_path(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.NGROK_MTLS)

    def test_ngrok_mtls_requires_ca_cert_to_exist(self) -> None:
        with self.assertRaises(ValueError):
            TransportConfig(
                mode=TransportMode.NGROK_MTLS,
                mtls_ca_cert_path=self.base / "missing.pem",
            )

    def test_ngrok_mtls_requires_pem_certificate(self) -> None:
        bogus = self.base / "bogus.pem"
        bogus.write_text("this is not a certificate\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            TransportConfig(mode=TransportMode.NGROK_MTLS, mtls_ca_cert_path=bogus)

    def test_ngrok_mtls_valid_config(self) -> None:
        config = TransportConfig(
            mode=TransportMode.NGROK_MTLS,
            mtls_ca_cert_path=pem_path(self.base),
            ngrok_authtoken="super-secret-token",
            ngrok_domain="bridge.example.com",
        )
        self.assertEqual(config.mode, TransportMode.NGROK_MTLS)

    def test_redacted_diagnostics_scrubs_token_and_cert(self) -> None:
        config = TransportConfig(
            mode=TransportMode.NGROK_MTLS,
            mtls_ca_cert_path=pem_path(self.base),
            ngrok_authtoken="super-secret-token",
            ngrok_domain="bridge.example.com",
        )
        diag = config.redacted_diagnostics()
        self.assertEqual(diag["ngrok_authtoken"], "<redacted>")
        self.assertNotIn("super-secret-token", str(diag))
        # Certificate content is never read or exposed.
        self.assertNotIn("BEGIN CERTIFICATE", str(diag))
        self.assertEqual(diag["mtls_ca_cert_path"], str(self.base / "ca.pem"))

    def test_redacted_diagnostics_no_token(self) -> None:
        config = TransportConfig(mode=TransportMode.STDIO)
        diag = config.redacted_diagnostics()
        self.assertIsNone(diag["ngrok_authtoken"])
        self.assertIsNone(diag["mtls_ca_cert_path"])


class StdioTransportAdapterTests(unittest.TestCase):
    def test_capabilities_and_health(self) -> None:
        adapter = StdioTransportAdapter()
        caps = adapter.capabilities()
        self.assertTrue(caps.is_local_only)
        self.assertEqual(caps.mode, TransportMode.STDIO)
        self.assertEqual(adapter.health()["status"], "healthy")
        # start/stop are no-ops that never raise.
        adapter.start()
        adapter.stop()

    def test_build_peer_metadata_is_stdio(self) -> None:
        adapter = StdioTransportAdapter()
        metadata = adapter.build_peer_metadata()
        self.assertEqual(metadata.transport_mode, TransportMode.STDIO)
        self.assertIsNone(metadata.remote_addr)
        self.assertFalse(metadata.mtls_verified)


class LocalHttpTransportAdapterTests(unittest.TestCase):
    def test_capabilities_are_loopback(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        caps = adapter.capabilities()
        self.assertTrue(caps.is_local_only)
        self.assertFalse(caps.requires_mtls)
        self.assertFalse(caps.requires_ngrok)
        self.assertFalse(caps.supports_peer_cert_identity)
        self.assertEqual(caps.bind_host, "127.0.0.1")
        self.assertEqual(caps.bind_port, 8000)

    def test_requires_local_http_mode(self) -> None:
        with self.assertRaises(ValueError):
            LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.STDIO))

    def test_probe_and_start_bind(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP, port=0))
        self.assertTrue(adapter.probe_bind_available())
        adapter.start()
        self.assertEqual(adapter.health()["status"], "healthy")
        self.assertTrue(adapter.health()["bound"])
        adapter.stop()
        self.assertEqual(adapter.health()["status"], "stopped")

    def test_start_rejects_bind_collision(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            adapter = LocalHttpTransportAdapter(
                TransportConfig(mode=TransportMode.LOCAL_HTTP, port=port)
            )
            self.assertFalse(adapter.probe_bind_available())
            with self.assertRaises(RuntimeError):
                adapter.start()
        finally:
            blocker.close()

    def test_rejects_non_loopback_remote_addr(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        with self.assertRaises(ValueError):
            adapter.build_peer_metadata(remote_addr="8.8.8.8")

    def test_accepts_loopback_remote_addr(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        metadata = adapter.build_peer_metadata(remote_addr="127.0.0.1")
        self.assertEqual(metadata.transport_mode, TransportMode.LOCAL_HTTP)
        self.assertEqual(metadata.remote_addr, "127.0.0.1")

    def test_rejects_untrusted_origin(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        with self.assertRaises(ValueError):
            adapter.build_peer_metadata(
                remote_addr="127.0.0.1", headers={"Origin": "https://evil.example"}
            )

    def test_accepts_trusted_origin(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        metadata = adapter.build_peer_metadata(
            remote_addr="127.0.0.1", headers={"Origin": "http://localhost"}
        )
        self.assertEqual(metadata.remote_addr, "127.0.0.1")

    def test_identity_headers_are_recorded_untrusted(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        metadata = adapter.build_peer_metadata(
            remote_addr="127.0.0.1",
            headers={
                "X-Forwarded-For": "1.2.3.4",
                "X-Client-Cert-SHA256": "deadbeef",
            },
        )
        untrusted = dict(metadata.untrusted_headers)
        self.assertIn("X-Forwarded-For", untrusted)
        self.assertIn("X-Client-Cert-SHA256", untrusted)

    def test_is_trusted_origin(self) -> None:
        adapter = LocalHttpTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))
        self.assertTrue(adapter.is_trusted_origin(None))
        self.assertTrue(adapter.is_trusted_origin("http://127.0.0.1"))
        self.assertFalse(adapter.is_trusted_origin("https://evil.example"))


class _FakeNgrokProcess:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.running = True

    def start(self) -> None:
        self.started = True
        self.running = True

    def stop(self) -> None:
        self.stopped = True
        self.running = False

    def poll(self) -> None | int:
        return None if self.running else 1


class NgrokMtlsTransportAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.config = TransportConfig(
            mode=TransportMode.NGROK_MTLS,
            mtls_ca_cert_path=pem_path(self.base),
            ngrok_authtoken="super-secret-token",
            ngrok_domain="bridge.example.com",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_requires_ngrok_mtls_mode(self) -> None:
        with self.assertRaises(ValueError):
            NgrokMtlsTransportAdapter(TransportConfig(mode=TransportMode.LOCAL_HTTP))

    def test_capabilities_require_ngrok_and_mtls(self) -> None:
        adapter = NgrokMtlsTransportAdapter(self.config)
        caps = adapter.capabilities()
        self.assertFalse(caps.is_local_only)
        self.assertTrue(caps.requires_ngrok)
        self.assertTrue(caps.requires_mtls)
        self.assertTrue(caps.supports_peer_cert_identity)

    def test_lifecycle_hooks_drive_process(self) -> None:
        process = _FakeNgrokProcess()
        adapter = NgrokMtlsTransportAdapter(self.config, process=process)
        self.assertEqual(adapter.health()["status"], "stopped")
        adapter.start()
        self.assertTrue(process.started)
        self.assertEqual(adapter.health()["status"], "healthy")
        adapter.stop()
        self.assertTrue(process.stopped)
        self.assertEqual(adapter.health()["status"], "stopped")

    def test_health_scrubs_token_and_cert(self) -> None:
        adapter = NgrokMtlsTransportAdapter(self.config)
        adapter.start()
        health = adapter.health()
        self.assertEqual(health["authtoken"], "<redacted>")
        self.assertNotIn("super-secret-token", str(health))
        self.assertNotIn("BEGIN CERTIFICATE", str(health))
        diag = adapter.redacted_diagnostics()
        self.assertEqual(diag["ngrok_authtoken"], "<redacted>")
        self.assertNotIn("BEGIN CERTIFICATE", str(diag))

    def test_build_peer_metadata_carries_mtls_evidence(self) -> None:
        adapter = NgrokMtlsTransportAdapter(self.config)
        metadata = adapter.build_peer_metadata(
            remote_addr="203.0.113.7",
            mtls_subject_dn="CN=operator,O=Example",
            mtls_cert_sha256="ab" * 32,
            mtls_verified=True,
        )
        self.assertEqual(metadata.transport_mode, TransportMode.NGROK_MTLS)
        self.assertTrue(metadata.mtls_verified)
        self.assertEqual(metadata.mtls_subject_dn, "CN=operator,O=Example")
        self.assertEqual(metadata.mtls_cert_sha256, "ab" * 32)


if __name__ == "__main__":
    unittest.main()
