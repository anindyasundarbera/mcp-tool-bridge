"""Unit tests for Conrad CLI Foundation & Service Lifecycle (M3.1, A3.1.1, A3.1.2).

Verifies:
- Exit code taxonomy and mapping for all ErrorFamily values.
- CLI argument parsing, --help, --version, and global flag ordering parity.
- JSON and human-readable output parity and secret/certificate redaction.
- Projects subcommand invoking GovernanceApplication core with valid/invalid configs.
- ServiceLifecycleManager start, status, stop lifecycle in foreground and background modes.
- Rejection of duplicate starts (STATE_CONFLICT = 7).
- Port conflict detection on LOCAL_HTTP (TRANSPORT_ERROR = 8).
- Safe stale PID recovery without signaling recycled or unrelated OS PIDs.
- Concurrent isolated instances across distinct state directories.
- Zero leftover residue after service shutdown.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from local_dev_mcp import __version__
from local_dev_mcp.cli import (
    CLIExitCode,
    CLIResponse,
    build_cli_parser,
    map_error_family_to_exit_code,
    redact_cli_data,
    render_cli_response,
    run_cli,
)
from local_dev_mcp.errors import ErrorFamily
from local_dev_mcp.service_lifecycle import (
    ServiceInstanceRecord,
    ServiceLifecycleManager,
    ServiceState,
    ServiceStatusReport,
)
from local_dev_mcp.transports import TransportMode


class TestCLIExitCodeAndRedaction(unittest.TestCase):
    """Test exit code mappings, redaction rules, and rendering parity."""

    def test_map_all_error_families(self) -> None:
        """Every single ErrorFamily member must map deterministically to a CLIExitCode."""
        for family in ErrorFamily:
            code = map_error_family_to_exit_code(family)
            self.assertIsInstance(code, CLIExitCode)
            self.assertNotEqual(code, CLIExitCode.OK)

        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.POLICY_DENIED), CLIExitCode.AUTHZ_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.AUTHORIZATION_FAILED), CLIExitCode.AUTHZ_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.VALIDATION_FAILED), CLIExitCode.CONFIG_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.NOT_FOUND), CLIExitCode.CONFIG_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.CONFLICT_OR_STALE), CLIExitCode.STATE_CONFLICT)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.VERIFICATION_FAILED), CLIExitCode.VERIFICATION_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.REVIEW_FAILED), CLIExitCode.VERIFICATION_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.PUBLICATION_FAILED), CLIExitCode.EXECUTOR_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.CAPTURE_FAILED), CLIExitCode.EXECUTOR_ERROR)
        self.assertEqual(map_error_family_to_exit_code(ErrorFamily.RUNTIME_ERROR), CLIExitCode.INTERNAL_ERROR)

    def test_redact_cli_data_bearer_tokens(self) -> None:
        """Bearer tokens in strings must be scrubbed."""
        input_data = {
            "auth": "Bearer secret-token-abc123xyz",
            "message": "Connected using Bearer tok_456789 successfully",
        }
        redacted = redact_cli_data(input_data)
        self.assertEqual(redacted["auth"], "Bearer <redacted>")
        self.assertEqual(redacted["message"], "Connected using Bearer <redacted> successfully")

    def test_redact_cli_data_pem_certificates(self) -> None:
        """PEM certificate blocks must be scrubbed."""
        pem_cert = (
            "-----BEGIN CERTIFICATE-----\n"
            "MIIDXTCCAkWgAwIBAgIJAJ0\n"
            "-----END CERTIFICATE-----"
        )
        input_data = {"cert": pem_cert, "nested": [pem_cert]}
        redacted = redact_cli_data(input_data)
        self.assertEqual(redacted["cert"], "<redacted>")
        self.assertEqual(redacted["nested"], ["<redacted>"])

    def test_redact_cli_data_sensitive_keys(self) -> None:
        """Keys matching secret patterns must be redacted regardless of case."""
        input_data = {
            "API_KEY": "12345",
            "slack_token": "secret_token",
            "my_password": "p@ssword",
            "AUTHTOKEN": "ngrok_token",
            "OH_SESSION_API_KEYS_DEV": "key_xyz",
            "public_key": "safe_val",
        }
        redacted = redact_cli_data(input_data)
        self.assertEqual(redacted["API_KEY"], "<redacted>")
        self.assertEqual(redacted["slack_token"], "<redacted>")
        self.assertEqual(redacted["my_password"], "<redacted>")
        self.assertEqual(redacted["AUTHTOKEN"], "<redacted>")
        self.assertEqual(redacted["OH_SESSION_API_KEYS_DEV"], "<redacted>")
        self.assertEqual(redacted["public_key"], "safe_val")

    def test_render_response_parity_and_redaction(self) -> None:
        """Both JSON and human rendering must redact sensitive data and preserve result."""
        response = CLIResponse(
            ok=True,
            command="test",
            exit_code=0,
            data={"token": "secret123", "public": "safe"},
            message="Operation succeeded with Bearer secret456",
        )

        json_out = render_cli_response(response, json_output=True)
        data_json = json.loads(json_out)
        self.assertTrue(data_json["ok"])
        self.assertEqual(data_json["data"]["token"], "<redacted>")
        self.assertEqual(data_json["data"]["public"], "safe")
        self.assertIn("Bearer <redacted>", data_json["message"])

        human_out = render_cli_response(response, json_output=False)
        self.assertIn("[test] Success (exit code: 0)", human_out)
        self.assertIn("Bearer <redacted>", human_out)
        self.assertIn("token: <redacted>", human_out)
        self.assertIn("public: safe", human_out)
        self.assertNotIn("secret123", human_out)
        self.assertNotIn("secret456", human_out)


class TestCLIParsingAndCommands(unittest.TestCase):
    """Test CLI commands, flag positions, --help, --version, and projects."""

    def test_help_flag_returns_ok(self) -> None:
        """--help and -h must display usage and exit with OK (0)."""
        out = io.StringIO()
        err = io.StringIO()
        code1 = run_cli(["--help"], stdout=out, stderr=err)
        self.assertEqual(code1, int(CLIExitCode.OK))
        self.assertIn("Conrad MCP Governance Bridge CLI", out.getvalue())

        out2 = io.StringIO()
        code2 = run_cli(["-h"], stdout=out2, stderr=err)
        self.assertEqual(code2, int(CLIExitCode.OK))

    def test_version_subcommand_and_flags(self) -> None:
        """Version information must be accessible via subcommand and flag."""
        out = io.StringIO()
        code = run_cli(["version", "--json"], stdout=out)
        self.assertEqual(code, int(CLIExitCode.OK))
        data = json.loads(out.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["version"], __version__)
        self.assertEqual(data["data"]["contract_version"], "1.0")

        # Flag parity before and after
        out2 = io.StringIO()
        code2 = run_cli(["--json", "version"], stdout=out2)
        self.assertEqual(code2, int(CLIExitCode.OK))
        self.assertEqual(json.loads(out2.getvalue())["data"]["version"], __version__)

        # Global --version flag
        out3 = io.StringIO()
        code3 = run_cli(["--version", "--json"], stdout=out3)
        self.assertEqual(code3, int(CLIExitCode.OK))
        self.assertEqual(json.loads(out3.getvalue())["data"]["version"], __version__)

    def test_projects_subcommand_missing_and_invalid_config(self) -> None:
        """Projects command must fail cleanly on missing or corrupt configuration."""
        err = io.StringIO()
        code_missing = run_cli(["--config", "/nonexistent/projects.json", "projects"], stderr=err)
        self.assertEqual(code_missing, int(CLIExitCode.CONFIG_ERROR))

        with tempfile.TemporaryDirectory() as tmp_dir:
            bad_config = Path(tmp_dir) / "corrupt.json"
            bad_config.write_text("{invalid_json: true", encoding="utf-8")
            err2 = io.StringIO()
            code_bad = run_cli(["--config", str(bad_config), "projects"], stderr=err2)
            self.assertEqual(code_bad, int(CLIExitCode.CONFIG_ERROR))

    def test_projects_subcommand_valid_config(self) -> None:
        """Projects command must invoke GovernanceApplication and list projects."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "test_repo"
            repo_root.mkdir()
            # Initialize minimal git repository so require_git check passes
            os.system(f"git -C {repo_root} init -q")

            config_file = Path(tmp_dir) / "projects.json"
            payload = {
                "version": 1,
                "projects": [
                    {
                        "id": "test-proj",
                        "display_name": "Test Project",
                        "description": "Test Project Description",
                        "root": str(repo_root),
                        "require_git": True,
                        "enabled": True,
                        "capabilities": ["repository_read", "policy_introspection"],
                        "markdown_write_roots": ["docs"],
                        "verification_commands": [],
                    }
                ],
            }
            config_file.write_text(json.dumps(payload), encoding="utf-8")

            out = io.StringIO()
            code = run_cli(["--config", str(config_file), "projects", "--json"], stdout=out)
            self.assertEqual(code, int(CLIExitCode.OK))
            resp = json.loads(out.getvalue())
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["data"]["count"], 1)
            self.assertEqual(resp["data"]["projects"][0]["project_id"], "test-proj")


class TestServiceLifecycleManager(unittest.TestCase):
    """Test service lifecycle start, status, stop, liveness, and isolation."""

    def test_foreground_mode_clean_exit_and_residue_cleanup(self) -> None:
        """Foreground mode must clean up PID file on exit and on error."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            config_path.write_text("{}", encoding="utf-8")
            state_dir = Path(tmp_dir) / "state"

            manager = ServiceLifecycleManager(
                config_path=config_path,
                state_dir=state_dir,
            )

            # Normal clean exit
            called = False
            def _runner() -> int:
                nonlocal called
                called = True
                # During runner execution, PID file must exist
                self.assertTrue(manager.pid_file.is_file())
                return 0

            report = manager.start(foreground=True, foreground_runner=_runner)
            self.assertTrue(called)
            self.assertEqual(report.state, ServiceState.STOPPED)
            # After exit, PID file must be unlinked (zero residue)
            self.assertFalse(manager.pid_file.exists())

            # Exception during runner execution
            def _failing_runner() -> int:
                raise RuntimeError("Worker crashed")

            with self.assertRaises(RuntimeError):
                manager.start(foreground=True, foreground_runner=_failing_runner)
            # Zero residue even on crash
            self.assertFalse(manager.pid_file.exists())

    def test_background_mode_lifecycle_and_rejection_of_duplicate(self) -> None:
        """Background start must record PID, prevent duplicate starts, and stop cleanly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            config_path.write_text("{}", encoding="utf-8")
            state_dir = Path(tmp_dir) / "state"

            fake_pid = 123456
            alive = True

            def _spawner(**kwargs: Any) -> int:
                return fake_pid

            def _checker(pid: int, fingerprint: str) -> bool:
                return alive and pid == fake_pid

            def _signal(pid: int, sig: int) -> None:
                nonlocal alive
                if pid == fake_pid:
                    alive = False

            manager = ServiceLifecycleManager(
                config_path=config_path,
                state_dir=state_dir,
                process_spawner=_spawner,
                pid_checker=_checker,
                signal_sender=_signal,
            )

            # 1. Start background service
            start_report = manager.start(foreground=False)
            self.assertEqual(start_report.state, ServiceState.RUNNING)
            self.assertEqual(start_report.pid, fake_pid)
            self.assertTrue(manager.pid_file.is_file())

            # 2. Duplicate start must raise RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                manager.start(foreground=False)
            self.assertIn("already running", str(ctx.exception))

            # 3. Status must report RUNNING
            status_report = manager.status()
            self.assertEqual(status_report.state, ServiceState.RUNNING)
            self.assertEqual(status_report.pid, fake_pid)

            # 4. Stop service
            stop_report = manager.stop()
            self.assertEqual(stop_report.state, ServiceState.STOPPED)
            self.assertFalse(manager.pid_file.exists())
            self.assertFalse(alive)

            # 5. Idempotent stop
            stop_report2 = manager.stop()
            self.assertEqual(stop_report2.state, ServiceState.STOPPED)

    def test_cli_duplicate_start_rejected(self) -> None:
        """CLI start must return STATE_CONFLICT (7) when instance is already running."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            config_path.write_text("{}", encoding="utf-8")
            state_dir = Path(tmp_dir) / "state"
            state_dir.mkdir()
            pid_file = state_dir / "conrad-service.json"

            # Create a live record with current PID which is guaranteed alive
            live_record = ServiceInstanceRecord(
                instance_id="inst_live",
                pid=os.getpid(),
                started_at="2026-01-01T00:00:00Z",
                transport_mode="stdio",
                host="127.0.0.1",
                port=None,
                config_path=str(config_path),
                state_dir=str(state_dir),
                command_fingerprint="python",
                version=__version__,
            )
            live_record.save(pid_file)

            err = io.StringIO()
            cli_code = run_cli(
                ["--config", str(config_path), "--state-dir", str(state_dir), "start"],
                stderr=err,
            )
            self.assertEqual(cli_code, int(CLIExitCode.STATE_CONFLICT))

    def test_stale_pid_recovery_without_signaling(self) -> None:
        """Stale or recycled PID must be recovered without signaling unrelated processes."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            state_dir = Path(tmp_dir) / "state"
            pid_file = state_dir / "conrad-service.json"

            signal_mock = MagicMock()
            manager = ServiceLifecycleManager(
                config_path=config_path,
                state_dir=state_dir,
                pid_checker=lambda pid, fp: False,  # Simulates dead or unrelated process
                signal_sender=signal_mock,
            )

            record = ServiceInstanceRecord(
                instance_id="inst_dead",
                pid=999999,
                started_at="2026-01-01T00:00:00Z",
                transport_mode="stdio",
                host="127.0.0.1",
                port=None,
                config_path=str(config_path),
                state_dir=str(state_dir),
                command_fingerprint="local_dev_mcp:stdio",
                version=__version__,
            )
            record.save(pid_file)
            self.assertTrue(pid_file.is_file())

            # Status must detect dead PID, remove it, and report recovered
            status = manager.status()
            self.assertEqual(status.state, ServiceState.STOPPED)
            self.assertTrue(status.stale_pid_recovered)
            self.assertFalse(pid_file.exists())
            signal_mock.assert_not_called()

            # Calling stop on stale PID file must never signal
            record.save(pid_file)
            stop_report = manager.stop()
            self.assertEqual(stop_report.state, ServiceState.STOPPED)
            self.assertTrue(stop_report.stale_pid_recovered)
            signal_mock.assert_not_called()

    def test_http_port_collision_detection(self) -> None:
        """Starting with LOCAL_HTTP on an occupied port must fail before spawning."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            state_dir = Path(tmp_dir) / "state"

            # Bind a socket to create port collision
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            occupied_port = sock.getsockname()[1]

            try:
                manager = ServiceLifecycleManager(
                    config_path=config_path,
                    state_dir=state_dir,
                )
                with self.assertRaises(OSError):
                    manager.start(
                        transport_mode=TransportMode.LOCAL_HTTP,
                        host="127.0.0.1",
                        port=occupied_port,
                    )

                # Test via CLI
                err = io.StringIO()
                cli_code = run_cli(
                    [
                        "--config", str(config_path),
                        "--state-dir", str(state_dir),
                        "start",
                        "--transport", "http",
                        "--port", str(occupied_port),
                    ],
                    stderr=err,
                )
                self.assertEqual(cli_code, int(CLIExitCode.TRANSPORT_ERROR))
            finally:
                sock.close()

    def test_parallel_isolated_instances(self) -> None:
        """Independent instances with distinct state directories must run concurrently."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "projects.json"
            config_path.write_text("{}", encoding="utf-8")

            state_dir_a = Path(tmp_dir) / "state_a"
            state_dir_b = Path(tmp_dir) / "state_b"

            alive_a = True
            alive_b = True

            manager_a = ServiceLifecycleManager(
                config_path=config_path,
                state_dir=state_dir_a,
                process_spawner=lambda **kw: 11111,
                pid_checker=lambda pid, fp: alive_a if pid == 11111 else False,
                signal_sender=lambda pid, sig: globals().update(alive_a=False) if pid == 11111 else None,
            )

            manager_b = ServiceLifecycleManager(
                config_path=config_path,
                state_dir=state_dir_b,
                process_spawner=lambda **kw: 22222,
                pid_checker=lambda pid, fp: alive_b if pid == 22222 else False,
                signal_sender=lambda pid, sig: globals().update(alive_b=False) if pid == 22222 else None,
            )

            # Start both
            rep_a = manager_a.start(foreground=False, port=8001)
            rep_b = manager_b.start(foreground=False, port=8002)

            self.assertEqual(rep_a.state, ServiceState.RUNNING)
            self.assertEqual(rep_b.state, ServiceState.RUNNING)

            self.assertTrue(manager_a.pid_file.is_file())
            self.assertTrue(manager_b.pid_file.is_file())

            # Stop instance A; instance B must remain running
            manager_a.stop()
            self.assertFalse(manager_a.pid_file.exists())
            self.assertEqual(manager_a.status().state, ServiceState.STOPPED)
            self.assertEqual(manager_b.status().state, ServiceState.RUNNING)
            self.assertTrue(manager_b.pid_file.is_file())

            # Stop instance B
            manager_b.stop()
            self.assertFalse(manager_b.pid_file.exists())
            self.assertEqual(manager_b.status().state, ServiceState.STOPPED)


if __name__ == "__main__":
    unittest.main()
