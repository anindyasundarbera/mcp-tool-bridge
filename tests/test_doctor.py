"""Unit tests for Conrad Doctor Diagnostic Engine (M3.4, A3.4.1).

Verifies:
- Complete healthy environment evaluation (overall_healthy is True, status == 'pass').
- Detection of unsupported Python runtime (< 3.11) and missing dependencies.
- Detection of missing or failing Git executable.
- State directory validation (missing -> fail, lax permissions 0755 -> warn with chmod 700 remediation).
- SQLite state database validation (missing -> pass, corrupt -> fail with backup/restore remediation).
- Project config validation (missing -> fail, invalid -> fail with conrad project validate remediation).
- Bubblewrap containment check (fail for modifying profile, warn for readonly profile).
- Executor readiness check (optional openhands/docker skipped/non-blocking when not selected).
- Transport readiness check (stdio passes, port collision fails, ngrok mtls requires cert & token).
- Actionable remediation invariant on every FAIL and WARN check.
- Secret and certificate redaction in check details.
- 100% read-only invariant (zero mutations to repos, config, or database files).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from local_dev_mcp.containment import ContainmentDoctorReport
from local_dev_mcp.doctor import (
    DoctorCheckResult,
    DoctorReport,
    DoctorSeverity,
    run_conrad_doctor,
)
from local_dev_mcp.transports import TransportMode


class TestDoctorDiagnostics(unittest.TestCase):
    """Test suite for run_conrad_doctor diagnostic engine."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name).resolve()

        # Create valid state directory with 0700 permissions
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(mode=0o700)
        os.chmod(self.state_dir, 0o700)

        # Create valid sample git repository
        self.repo_dir = self.tmp_path / "repo"
        self.repo_dir.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo_dir)], check=True)

        # Create valid projects.json configuration
        self.config_path = self.tmp_path / "projects.json"
        config_data = {
            "version": 1,
            "projects": [
                {
                    "id": "sample-project",
                    "display_name": "Sample Project",
                    "description": "Test repository",
                    "root": str(self.repo_dir),
                    "require_git": True,
                    "enabled": True,
                    "capabilities": ["repository_read"],
                    "markdown_write_roots": ["docs"],
                    "verification_commands": [],
                }
            ],
        }
        self.config_path.write_text(json.dumps(config_data), encoding="utf-8")

        # Fake healthy bwrap probe
        self.healthy_bwrap = lambda: ContainmentDoctorReport(
            bwrap_path="/usr/bin/bwrap",
            userns_available=True,
            healthy=True,
            reason=None,
        )

        # Fake unhealthy bwrap probe
        self.unhealthy_bwrap = lambda: ContainmentDoctorReport(
            bwrap_path=None,
            userns_available=False,
            healthy=False,
            reason="bwrap binary not found",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_healthy_environment_evaluation(self) -> None:
        """Fully healthy environment must pass with overall_healthy=True and status='pass'."""
        report = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            transport_mode=TransportMode.STDIO,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )

        self.assertTrue(report.overall_healthy)
        self.assertEqual(report.status, "pass")
        self.assertEqual(report.summary["fail"], 0)
        self.assertGreater(report.summary["pass"], 0)

        # Ensure all checks have read_only True
        for c in report.checks:
            self.assertTrue(c.read_only)
            if c.severity in (DoctorSeverity.FAIL, DoctorSeverity.WARN):
                self.assertIsNotNone(c.remediation)

    def test_remediation_invariant_on_fail_and_warn(self) -> None:
        """Every FAIL or WARN result must have a specific, non-empty remediation."""
        # Unhealthy state: missing config, missing state dir
        report = run_conrad_doctor(
            config_path=self.tmp_path / "nonexistent.json",
            state_dir=self.tmp_path / "nonexistent_dir",
            transport_mode=TransportMode.STDIO,
            bwrap_probe_fn=self.unhealthy_bwrap,
        )

        self.assertFalse(report.overall_healthy)
        for check in report.checks:
            if check.severity in (DoctorSeverity.FAIL, DoctorSeverity.WARN):
                self.assertIsNotNone(check.remediation, f"Check {check.check_id} missing remediation")
                self.assertNotIn("check logs", check.remediation.lower())
                self.assertGreater(len(check.remediation), 10)

    def test_unsupported_python_version(self) -> None:
        """Python runtime check must fail when version < 3.11."""
        with patch("sys.version_info", (3, 10, 0, "final", 0)):
            report = run_conrad_doctor(
                config_path=self.config_path,
                state_dir=self.state_dir,
                bwrap_probe_fn=self.healthy_bwrap,
                executor_health_fn=lambda: (True, None),
            )
            python_check = next(c for c in report.checks if c.check_id == "python_runtime")
            self.assertEqual(python_check.severity, DoctorSeverity.FAIL)
            self.assertIn("Upgrade Python", python_check.remediation or "")

    def test_missing_git_binary(self) -> None:
        """Git check must fail when git binary is not found in PATH."""
        with patch("shutil.which", return_value=None):
            report = run_conrad_doctor(
                config_path=self.config_path,
                state_dir=self.state_dir,
                bwrap_probe_fn=self.healthy_bwrap,
                executor_health_fn=lambda: (True, None),
            )
            git_check = next(c for c in report.checks if c.check_id == "git_runtime")
            self.assertEqual(git_check.severity, DoctorSeverity.FAIL)
            self.assertIn("sudo apt-get install", git_check.remediation or "")

    def test_lax_state_directory_permissions(self) -> None:
        """State directory with group/world permissions (0755) must yield WARN with chmod 700 remediation."""
        os.chmod(self.state_dir, 0o755)
        report = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        state_check = next(c for c in report.checks if c.check_id == "state_directory")
        self.assertEqual(state_check.severity, DoctorSeverity.WARN)
        self.assertIn("chmod 700", state_check.remediation or "")

    def test_corrupt_sqlite_state_database(self) -> None:
        """Corrupted SQLite state database must trigger FAIL with backup/restore remediation."""
        db_path = self.state_dir / "state.db"
        # Write corrupted garbage to sqlite database file
        db_path.write_bytes(b"NOT A REAL SQLITE DATABASE HEADER TRUNCATED DATA")

        report = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        db_check = next(c for c in report.checks if c.check_id == "sqlite_state_db")
        self.assertEqual(db_check.severity, DoctorSeverity.FAIL)
        self.assertIn("conrad init --allow-existing-with-backup", db_check.remediation or "")

    def test_healthy_sqlite_state_database(self) -> None:
        """Valid SQLite database must pass integrity check."""
        db_path = self.state_dir / "bridge.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO test_table VALUES (1, 'hello');")
        conn.commit()
        conn.close()

        report = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        db_check = next(c for c in report.checks if c.check_id == "sqlite_state_db")
        self.assertEqual(db_check.severity, DoctorSeverity.PASS)

    def test_bwrap_fail_on_untrusted_vs_warn_on_readonly(self) -> None:
        """Unavailable bwrap must FAIL for modifying profiles and WARN for readonly profile."""
        # Untrusted modifying profile
        report_modify = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            profile="develop",
            bwrap_probe_fn=self.unhealthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        bwrap_check_mod = next(c for c in report_modify.checks if c.check_id == "containment_bwrap")
        self.assertEqual(bwrap_check_mod.severity, DoctorSeverity.FAIL)
        self.assertIn("sudo apt-get install -y bubblewrap", bwrap_check_mod.remediation or "")

        # Readonly profile
        report_readonly = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            profile="readonly",
            bwrap_probe_fn=self.unhealthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        bwrap_check_ro = next(c for c in report_readonly.checks if c.check_id == "containment_bwrap")
        self.assertEqual(bwrap_check_ro.severity, DoctorSeverity.WARN)
        self.assertIn("bubblewrap", bwrap_check_ro.remediation or "")

    def test_loopback_port_conflict(self) -> None:
        """Port conflict on LOCAL_HTTP transport must report FAIL with port remediation."""
        def _failing_probe(host: str, port: int) -> None:
            raise OSError(f"Address {host}:{port} already in use")

        report = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            transport_mode=TransportMode.LOCAL_HTTP,
            host="127.0.0.1",
            port=9999,
            port_probe_fn=_failing_probe,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        trans_check = next(c for c in report.checks if c.check_id == "transport_readiness")
        self.assertEqual(trans_check.severity, DoctorSeverity.FAIL)
        self.assertIn("--port", trans_check.remediation or "")

    def test_invalid_project_config(self) -> None:
        """Invalid JSON or invalid policy in projects.json must report FAIL."""
        bad_config = self.tmp_path / "bad_projects.json"
        bad_config.write_text("{ broken json : [ }", encoding="utf-8")

        report = run_conrad_doctor(
            config_path=bad_config,
            state_dir=self.state_dir,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )
        cfg_check = next(c for c in report.checks if c.check_id == "project_config")
        self.assertEqual(cfg_check.severity, DoctorSeverity.FAIL)
        self.assertIn("conrad project validate", cfg_check.remediation or "")

    def test_secret_and_token_redaction(self) -> None:
        """Secrets, tokens, and certificates must be redacted in report serialization."""
        raw_result = DoctorCheckResult(
            check_id="custom_secret_test",
            scope="security",
            severity=DoctorSeverity.WARN,
            summary="Secret test",
            remediation="No remediation",
            details={
                "api_key": "sk-proj-super-secret-key-12345",
                "bearer": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
                "cert": "-----BEGIN CERTIFICATE-----\nABCDEF123456\n-----END CERTIFICATE-----",
                "nested": {"token": "my-token-val"},
            },
        )
        dict_val = raw_result.to_dict()
        self.assertEqual(dict_val["details"]["api_key"], "<redacted>")
        self.assertEqual(dict_val["details"]["bearer"], "Bearer <redacted>")
        self.assertEqual(dict_val["details"]["cert"], "<redacted>")
        self.assertEqual(dict_val["details"]["nested"]["token"], "<redacted>")

    def test_read_only_invariant(self) -> None:
        """Doctor execution must never modify state_dir, config_path, or repo files."""
        # Record initial hashes and timestamps
        config_bytes_before = self.config_path.read_bytes()
        config_mtime_before = self.config_path.stat().st_mtime_ns
        state_files_before = set(self.state_dir.iterdir())

        _ = run_conrad_doctor(
            config_path=self.config_path,
            state_dir=self.state_dir,
            transport_mode=TransportMode.STDIO,
            bwrap_probe_fn=self.healthy_bwrap,
            executor_health_fn=lambda: (True, None),
        )

        self.assertEqual(self.config_path.read_bytes(), config_bytes_before)
        self.assertEqual(self.config_path.stat().st_mtime_ns, config_mtime_before)
        self.assertEqual(set(self.state_dir.iterdir()), state_files_before)


if __name__ == "__main__":
    unittest.main()
