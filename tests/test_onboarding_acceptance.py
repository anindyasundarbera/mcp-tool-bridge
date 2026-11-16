"""Unit and acceptance tests for Newcomer Onboarding Acceptance Suite (M3.5, A3.5.1).

Verifies:
- End-to-end newcomer onboarding acceptance flow passes in seconds.
- Performance is strictly within the five-minute target (total_duration_ms < 300,000).
- Zero residual background processes and zero PID file residue on completion.
- Rejection of existing configuration collision without --allow-existing-with-backup.
- Proper detection of invalid repository paths producing actionable blockers.
- CLI execution parity via `conrad onboard-check --json`.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from local_dev_mcp.cli import CLIExitCode, run_cli
from local_dev_mcp.onboarding import (
    OnboardingAcceptanceReport,
    OnboardingStepRecord,
    run_onboarding_acceptance,
)


class TestOnboardingAcceptance(unittest.TestCase):
    """Acceptance test suite for newcomer onboarding experience."""

    def test_full_newcomer_onboarding_flow(self) -> None:
        """Newcomer onboarding flow must complete successfully with zero residue."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_path = Path(tmp_dir).resolve()
            report = run_onboarding_acceptance(ws_path, keep_artifacts=True)

            self.assertTrue(report.passed, f"Onboarding failed with blockers: {report.blockers}")
            self.assertTrue(report.within_five_minute_target)
            self.assertEqual(len(report.blockers), 0)

            # Check that all required steps ran and passed
            step_ids = [s.step_id for s in report.steps]
            self.assertIn("version", step_ids)
            self.assertIn("init", step_ids)
            self.assertIn("project_add", step_ids)
            self.assertIn("project_validate", step_ids)
            self.assertIn("doctor", step_ids)
            self.assertIn("lifecycle_start", step_ids)
            self.assertIn("lifecycle_status", step_ids)
            self.assertIn("lifecycle_stop", step_ids)

            for step in report.steps:
                self.assertTrue(step.ok, f"Step {step.step_id} failed: {step.summary}")
                self.assertEqual(step.exit_code, 0)
                self.assertGreaterEqual(step.duration_ms, 0)

            # Verify that service PID file residue does not exist
            pid_file = ws_path / "state" / "conrad-service.json"
            self.assertFalse(pid_file.exists())

    def test_existing_config_collision_without_backup(self) -> None:
        """Initialization must fail when configuration already exists and backup flag is omitted."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_path = Path(tmp_dir).resolve()
            config_path = ws_path / "projects.json"
            config_path.write_text("{\"existing\": true}", encoding="utf-8")

            report = run_onboarding_acceptance(ws_path, keep_artifacts=True)

            self.assertFalse(report.passed)
            self.assertGreater(len(report.blockers), 0)
            self.assertTrue(any("init" in b.lower() or "conflict" in b.lower() for b in report.blockers))

    def test_invalid_repo_path_detection(self) -> None:
        """Registering a non-existent repository must produce an actionable blocker."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_path = Path(tmp_dir).resolve()
            # Initialize config first
            out_buf = io.StringIO()
            run_cli(["init", "--config", str(ws_path / "projects.json"), "--state-dir", str(ws_path / "state"), "--json"], stdout=out_buf)

            # Try to add a non-existent repository
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            code = run_cli(
                [
                    "project",
                    "add",
                    "--id",
                    "missing-repo",
                    "--root",
                    str(ws_path / "does_not_exist"),
                    "--config",
                    str(ws_path / "projects.json"),
                    "--json",
                ],
                stdout=out_buf,
                stderr=err_buf,
            )
            self.assertEqual(code, int(CLIExitCode.CONFIG_ERROR))

    def test_onboard_check_cli_subcommand(self) -> None:
        """CLI invocation of onboard-check must execute suite and report structured JSON."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws_path = Path(tmp_dir).resolve()
            out_buf = io.StringIO()
            code = run_cli(
                ["onboard-check", "--workspace", str(ws_path), "--keep-artifacts", "--json"],
                stdout=out_buf,
            )
            self.assertEqual(code, int(CLIExitCode.OK))

            payload = json.loads(out_buf.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "onboard-check")
            self.assertTrue(payload["data"]["passed"])
            self.assertTrue(payload["data"]["within_five_minute_target"])


if __name__ == "__main__":
    unittest.main()
