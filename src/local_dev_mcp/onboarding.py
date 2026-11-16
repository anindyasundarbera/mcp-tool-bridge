"""Newcomer Onboarding Acceptance Suite (M3.5 / A3.5.1).

Executes the complete Conrad newcomer onboarding workflow in an isolated
workspace using only CLI commands with --json output mode:
1. Version inquiry (conrad version --json)
2. Fresh initialization (conrad init --config ... --state-dir ...)
3. Project registration (conrad project add ...)
4. Registry validation (conrad project validate ...)
5. Prerequisites diagnostics (conrad doctor ...)
6. Service lifecycle verification (conrad start -> status -> stop)

Verifies zero residue, non-interactive execution, zero ngrok requirements,
deterministic exit codes, and < 5 minute runtime target.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from .cli import CLIExitCode, redact_cli_data, run_cli


@dataclass(frozen=True, slots=True)
class OnboardingStepRecord:
    """Record of an individual executed step within the onboarding acceptance suite."""

    step_id: str
    command: tuple[str, ...]
    exit_code: int
    duration_ms: int
    ok: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        """Convert step record to JSON-safe dictionary with redacted secrets."""
        raw = {
            "step_id": self.step_id,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "summary": self.summary,
        }
        return redact_cli_data(raw)  # type: ignore[no-any-return]


@dataclass(frozen=True, slots=True)
class OnboardingAcceptanceReport:
    """Consolidated report of the newcomer onboarding acceptance evaluation."""

    passed: bool
    total_duration_ms: int
    within_five_minute_target: bool
    steps: tuple[OnboardingStepRecord, ...]
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Convert acceptance report to JSON-safe dictionary with redacted secrets."""
        raw = {
            "passed": self.passed,
            "total_duration_ms": self.total_duration_ms,
            "within_five_minute_target": self.within_five_minute_target,
            "steps": [s.to_dict() for s in self.steps],
            "blockers": list(self.blockers),
        }
        return redact_cli_data(raw)  # type: ignore[no-any-return]


def _exec_cli_step(
    step_id: str,
    argv: Sequence[str],
) -> tuple[OnboardingStepRecord, dict[str, Any] | None]:
    """Execute a single CLI command with timing and output capture."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    start_ns = time.monotonic_ns()

    try:
        code = run_cli(argv, stdout=out_buf, stderr=err_buf)
    except Exception as exc:
        duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        return (
            OnboardingStepRecord(
                step_id=step_id,
                command=tuple(argv),
                exit_code=int(CLIExitCode.INTERNAL_ERROR),
                duration_ms=duration_ms,
                ok=False,
                summary=f"Execution raised unhandled exception: {exc}",
            ),
            None,
        )

    duration_ms = (time.monotonic_ns() - start_ns) // 1_000_000
    stdout_text = out_buf.getvalue().strip()
    stderr_text = err_buf.getvalue().strip()

    parsed_json: dict[str, Any] | None = None
    target_output = stdout_text or stderr_text
    if target_output:
        try:
            parsed_json = json.loads(target_output)
        except Exception:
            parsed_json = None

    ok = (code == int(CLIExitCode.OK))
    if parsed_json and isinstance(parsed_json, dict) and not parsed_json.get("ok", True):
        ok = False

    summary = ""
    if parsed_json and isinstance(parsed_json, dict):
        summary = str(parsed_json.get("message", ""))
    if not summary:
        summary = f"Exit code {code}"
        if stderr_text:
            summary += f": {stderr_text[:120]}"

    record = OnboardingStepRecord(
        step_id=step_id,
        command=tuple(argv),
        exit_code=code,
        duration_ms=duration_ms,
        ok=ok,
        summary=summary,
    )
    return record, parsed_json


def run_onboarding_acceptance(
    workspace_dir: Path | str,
    *,
    keep_artifacts: bool = True,
) -> OnboardingAcceptanceReport:
    """Run full newcomer onboarding flow inside an isolated workspace directory."""
    ws = Path(workspace_dir).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)

    config_path = ws / "projects.json"
    state_dir = ws / "state"
    sample_repo_dir = ws / "sample-repo"

    steps: list[OnboardingStepRecord] = []
    blockers: list[str] = []
    service_started = False

    start_suite_ns = time.monotonic_ns()

    try:
        # Step 1: version check
        rec_ver, data_ver = _exec_cli_step(
            "version",
            ["version", "--json"],
        )
        steps.append(rec_ver)
        if not rec_ver.ok:
            blockers.append(f"version check failed: {rec_ver.summary}")

        # Step 2: fresh initialization
        rec_init, data_init = _exec_cli_step(
            "init",
            [
                "init",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
        )
        steps.append(rec_init)
        if not rec_init.ok:
            blockers.append(f"init step failed: {rec_init.summary}")

        # Step 3: initialize sample git repository and register project
        sample_repo_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "init", "-q", str(sample_repo_dir)], check=True)
            readme = sample_repo_dir / "README.md"
            readme.write_text("# Sample Project\nInitial onboarding test.\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(sample_repo_dir), "add", "README.md"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(sample_repo_dir),
                    "-c",
                    "user.name=Conrad",
                    "-c",
                    "user.email=conrad@example.local",
                    "commit",
                    "-m",
                    "Initial commit",
                ],
                check=True,
            )
        except Exception as exc:
            blockers.append(f"Git sample repository setup failed: {exc}")

        rec_proj_add, data_proj_add = _exec_cli_step(
            "project_add",
            [
                "project",
                "add",
                "--id",
                "sample-repo",
                "--root",
                str(sample_repo_dir),
                "--profile",
                "develop-verify",
                "--verify-cmd",
                "unit:/bin/true",
                "--config",
                str(config_path),
                "--json",
            ],
        )
        steps.append(rec_proj_add)
        if not rec_proj_add.ok:
            blockers.append(f"project add failed: {rec_proj_add.summary}")

        # Step 4: project validation
        rec_proj_val, data_proj_val = _exec_cli_step(
            "project_validate",
            [
                "project",
                "validate",
                "--config",
                str(config_path),
                "--json",
            ],
        )
        steps.append(rec_proj_val)
        if not rec_proj_val.ok:
            blockers.append(f"project validate failed: {rec_proj_val.summary}")

        # Step 5: doctor diagnostics
        rec_doc, data_doc = _exec_cli_step(
            "doctor",
            [
                "doctor",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
        )
        steps.append(rec_doc)
        if not rec_doc.ok:
            blockers.append(f"doctor check failed: {rec_doc.summary}")
        elif data_doc and not data_doc.get("data", {}).get("overall_healthy", True):
            blockers.append("doctor reported unhealthy environment prerequisites")

        # Step 6: service lifecycle: start -> status -> stop
        rec_start, data_start = _exec_cli_step(
            "lifecycle_start",
            [
                "start",
                "--background",
                "--transport",
                "stdio",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
        )
        steps.append(rec_start)
        if rec_start.ok:
            service_started = True
        else:
            blockers.append(f"service start failed: {rec_start.summary}")

        # Probe status
        rec_status, data_status = _exec_cli_step(
            "lifecycle_status",
            [
                "status",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
        )
        steps.append(rec_status)
        if not rec_status.ok:
            blockers.append(f"service status check failed: {rec_status.summary}")
        else:
            state_val = (data_status or {}).get("data", {}).get("state")
            if state_val != "running":
                blockers.append(f"service status expected 'running', got '{state_val}'")

        # Stop service
        rec_stop, data_stop = _exec_cli_step(
            "lifecycle_stop",
            [
                "stop",
                "--config",
                str(config_path),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
        )
        steps.append(rec_stop)
        if not rec_stop.ok:
            blockers.append(f"service stop failed: {rec_stop.summary}")
        else:
            service_started = False
            # Verify PID file residue is cleared
            pid_file = state_dir / "conrad-service.json"
            if pid_file.exists():
                blockers.append(f"PID residue file still exists after stop: {pid_file}")

    finally:
        # Guarantee service stop cleanup if an unhandled failure occurred
        if service_started:
            try:
                _exec_cli_step(
                    "emergency_stop",
                    [
                        "stop",
                        "--config",
                        str(config_path),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ],
                )
            except Exception:
                pass

        if not keep_artifacts:
            try:
                shutil.rmtree(ws, ignore_errors=True)
            except Exception:
                pass

    total_duration_ms = (time.monotonic_ns() - start_suite_ns) // 1_000_000
    within_five_minute_target = total_duration_ms < 300_000
    passed = len(blockers) == 0 and all(s.ok for s in steps)

    return OnboardingAcceptanceReport(
        passed=passed,
        total_duration_ms=total_duration_ms,
        within_five_minute_target=within_five_minute_target,
        steps=tuple(steps),
        blockers=tuple(blockers),
    )


__all__ = [
    "OnboardingAcceptanceReport",
    "OnboardingStepRecord",
    "run_onboarding_acceptance",
]
