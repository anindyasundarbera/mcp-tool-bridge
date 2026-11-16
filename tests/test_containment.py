"""Tests for executor containment primitives and bwrap process runner (A2.2.2)."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from local_dev_mcp.containment import (
    ContainmentDoctorReport,
    ContainmentMode,
    ContainmentPolicy,
    ContainmentProfile,
    ContainmentUnavailableError,
    ContainedProcessRunner,
    SAFE_MINIMAL_ENV_KEYS,
    build_bwrap_argv,
    build_executor_environment,
    build_minimal_executor_env,
    compute_modified_paths,
    containment_profile_for,
    is_path_within_candidate,
    probe_bwrap_containment,
    validate_candidate_isolation,
)
from local_dev_mcp.executor_contract import (
    ExecutionRequest,
    ExecutorResourceLimits,
    NetworkMode,
)


class MinimalEnvironmentScrubbingTests(unittest.TestCase):
    def test_minimal_env_strips_host_secrets(self) -> None:
        host_env = {
            "SECRET_CANARY_TOKEN": "canary-secret-12345",
            "OPENAI_API_KEY": "sk-openai-secret-token",
            "ANTHROPIC_API_KEY": "ant-secret-token",
            "NGROK_AUTHTOKEN": "ngrok-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-key",
            "LOCAL_DEV_MCP_AUTH_KEY": "bridge-signing-key",
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/tester",
            "LANG": "en_US.UTF-8",
            "USER": "tester",
        }
        cleaned = build_minimal_executor_env(host_env)

        self.assertNotIn("SECRET_CANARY_TOKEN", cleaned)
        self.assertNotIn("OPENAI_API_KEY", cleaned)
        self.assertNotIn("ANTHROPIC_API_KEY", cleaned)
        self.assertNotIn("NGROK_AUTHTOKEN", cleaned)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", cleaned)
        self.assertNotIn("LOCAL_DEV_MCP_AUTH_KEY", cleaned)

        self.assertEqual(cleaned["PATH"], "/usr/bin:/bin")
        self.assertEqual(cleaned["HOME"], "/home/tester")
        self.assertEqual(cleaned["LANG"], "en_US.UTF-8")
        self.assertEqual(cleaned["USER"], "tester")

    def test_minimal_env_projects_explicit_allowed_keys(self) -> None:
        host_env = {
            "SECRET_CANARY_TOKEN": "strip-me",
            "PATH": "/usr/bin:/bin",
        }
        explicit = {
            "EXPLICIT_ALLOWED_VAR": "projected-value",
            "ANOTHER_VAR": "42",
        }
        cleaned = build_minimal_executor_env(host_env, explicit_projection=explicit)

        self.assertNotIn("SECRET_CANARY_TOKEN", cleaned)
        self.assertEqual(cleaned["EXPLICIT_ALLOWED_VAR"], "projected-value")
        self.assertEqual(cleaned["ANOTHER_VAR"], "42")
        self.assertEqual(cleaned["PATH"], "/usr/bin:/bin")

    def test_safe_minimal_env_keys_content(self) -> None:
        for expected in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "LOGNAME"):
            self.assertIn(expected, SAFE_MINIMAL_ENV_KEYS)


class BwrapArgvConstructionTests(unittest.TestCase):
    def test_network_none_omits_share_net(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(network_mode=NetworkMode.NONE)
            argv = build_bwrap_argv(
                ["echo", "hi"],
                candidate_root=cand,
                policy=policy,
                env={"TEST": "1"},
            )
            self.assertNotIn("--share-net", argv)
            self.assertIn("--clearenv", argv)
            self.assertIn("--setenv", argv)
            self.assertIn(str(cand), argv)
            self.assertEqual(argv[-2:], ["echo", "hi"])

    def test_network_loopback_adds_share_net(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(network_mode=NetworkMode.LOOPBACK_ONLY)
            argv = build_bwrap_argv(
                ["echo", "hi"],
                candidate_root=cand,
                policy=policy,
                env={},
            )
            self.assertIn("--share-net", argv)

    def test_canonical_root_masked_and_binds_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            canon = Path(td) / "canon"
            cand.mkdir()
            canon.mkdir()
            policy = ContainmentPolicy()
            argv = build_bwrap_argv(
                ["ls"],
                candidate_root=cand,
                canonical_root=canon,
                policy=policy,
                env={},
            )
            # Check canonical root is masked via tmpfs
            self.assertIn("--tmpfs", argv)
            canon_idx = argv.index(str(canon))
            self.assertEqual(argv[canon_idx - 1], "--tmpfs")

            # Check candidate root is writable bind
            cand_idx = argv.index(str(cand))
            self.assertEqual(argv[cand_idx - 1], "--bind")

    def test_forbidden_paths_masked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            forbidden_dir = Path(td) / "forbidden_dir"
            cand.mkdir()
            forbidden_dir.mkdir()
            policy = ContainmentPolicy(forbidden_paths=(forbidden_dir,))
            argv = build_bwrap_argv(
                ["ls"],
                candidate_root=cand,
                policy=policy,
                env={},
            )
            idx = argv.index(str(forbidden_dir))
            self.assertEqual(argv[idx - 1], "--tmpfs")


class ContainedProcessRunnerPolicyTests(unittest.TestCase):
    def test_fail_closed_when_bwrap_unavailable_without_fallback(self) -> None:
        runner = ContainedProcessRunner(
            doctor_report=ContainmentDoctorReport(
                bwrap_path=None,
                userns_available=False,
                healthy=False,
                reason="bwrap binary missing",
            )
        )
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(
                mode=ContainmentMode.BWRAP,
                allow_uncontained_fallback=False,
            )
            with self.assertRaises(ContainmentUnavailableError):
                runner.run(["echo", "hello"], candidate_root=cand, policy=policy)

    def test_loud_uncontained_trusted_dev_profile_on_explicit_mode(self) -> None:
        runner = ContainedProcessRunner()
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(mode=ContainmentMode.TRUSTED_DEV_UNCONTAINED)
            res = runner.run(["echo", "hello"], candidate_root=cand, policy=policy)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout.strip(), b"hello")
            self.assertEqual(res.containment_profile, "UNCONTAINED_TRUSTED_DEV")
            self.assertTrue(res.orphan_cleanup_ran)

    def test_fallback_allowed_uses_uncontained_trusted_dev_profile(self) -> None:
        runner = ContainedProcessRunner(
            doctor_report=ContainmentDoctorReport(
                bwrap_path=None,
                userns_available=False,
                healthy=False,
                reason="bwrap probe simulated failure",
            )
        )
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(
                mode=ContainmentMode.BWRAP,
                allow_uncontained_fallback=True,
            )
            res = runner.run(["echo", "fallback"], candidate_root=cand, policy=policy)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.containment_profile, "UNCONTAINED_TRUSTED_DEV")
            self.assertTrue(res.orphan_cleanup_ran)


class LiveBwrapContainmentHostileTests(unittest.TestCase):
    """Real hostile child execution tests under live Linux bwrap containment."""

    def setUp(self) -> None:
        self.doctor = probe_bwrap_containment()
        if not self.doctor.healthy:
            self.skipTest(f"Live bwrap containment unavailable on host: {self.doctor.reason}")

    def test_reading_undeclared_env_fails(self) -> None:
        runner = ContainedProcessRunner()
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(mode=ContainmentMode.BWRAP)

            with patch.dict(os.environ, {"SECRET_CANARY_TOKEN": "super-secret-123"}):
                env = build_minimal_executor_env(os.environ, explicit_projection={"ALLOWED": "ok"})
                cmd = [
                    "python3",
                    "-c",
                    "import os, sys; sys.exit(0 if os.environ.get('SECRET_CANARY_TOKEN') is None and os.environ.get('ALLOWED') == 'ok' else 1)",
                ]
                res = runner.run(cmd, candidate_root=cand, policy=policy, env=env)
                self.assertEqual(res.returncode, 0)
                self.assertEqual(res.containment_profile, "bwrap")

    def test_writing_to_canonical_checkout_fails(self) -> None:
        runner = ContainedProcessRunner()
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            canon = Path(td) / "canon"
            cand.mkdir()
            canon.mkdir()
            canary_file = canon / "canary.txt"
            canary_file.write_text("pristine canonical content\n", encoding="utf-8")

            policy = ContainmentPolicy(mode=ContainmentMode.BWRAP)
            # Hostile child attempts to overwrite canary or create new files in canonical root
            target_file = canon / "breach.txt"
            cmd = [
                "python3",
                "-c",
                f"import sys\n"
                f"try:\n"
                f"    with open('{target_file}', 'w') as f:\n"
                f"        f.write('breached!')\n"
                f"    with open('{canary_file}', 'w') as f:\n"
                f"        f.write('overwritten!')\n"
                f"except Exception:\n"
                f"    pass\n",
            ]
            res = runner.run(cmd, candidate_root=cand, canonical_root=canon, policy=policy, env={})
            self.assertEqual(res.returncode, 0)

            # Assert host canonical root is unmodified!
            self.assertEqual(canary_file.read_text(encoding="utf-8"), "pristine canonical content\n")
            self.assertFalse(target_file.exists())

    def test_network_egress_under_network_none_fails(self) -> None:
        runner = ContainedProcessRunner()
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            policy = ContainmentPolicy(mode=ContainmentMode.BWRAP, network_mode=NetworkMode.NONE)

            # Hostile child attempts external network egress to 1.1.1.1
            cmd = [
                "python3",
                "-c",
                "import urllib.request, sys\n"
                "try:\n"
                "    urllib.request.urlopen('http://1.1.1.1', timeout=1)\n"
                "    sys.exit(0)\n"
                "except Exception:\n"
                "    sys.exit(42)\n",
            ]
            res = runner.run(cmd, candidate_root=cand, policy=policy, env={})
            self.assertEqual(res.returncode, 42)

    def test_spawning_background_sleep_killed_leaving_zero_orphans(self) -> None:
        runner = ContainedProcessRunner()
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            cand.mkdir()
            pid_file = Path(td) / "orphan_pid.txt"

            policy = ContainmentPolicy(mode=ContainmentMode.BWRAP)
            cmd = [
                "bash",
                "-c",
                f"sleep 60 & echo $! > {pid_file}; exit 0",
            ]
            res = runner.run(cmd, candidate_root=cand, policy=policy, env={})
            self.assertEqual(res.returncode, 0)
            self.assertTrue(res.orphan_cleanup_ran)

            time.sleep(0.1)
            if pid_file.exists() and pid_file.read_text().strip():
                child_pid = int(pid_file.read_text().strip())
                # Check whether child_pid is dead on host
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)


class ContainmentHelperTests(unittest.TestCase):
    def test_is_path_within_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td)
            self.assertTrue(is_path_within_candidate(cand, "file.txt"))
            self.assertTrue(is_path_within_candidate(cand, "sub/dir/file.txt"))
            self.assertFalse(is_path_within_candidate(cand, "../escape.txt"))
            self.assertFalse(is_path_within_candidate(cand, "/etc/passwd"))
            self.assertFalse(is_path_within_candidate(cand, "./local.txt"))
            self.assertFalse(is_path_within_candidate(cand, ""))

    def test_compute_modified_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td)
            before = {"working_tree": ""}
            after = {"working_tree": " M src/mod.py\n?? new_file.txt\n"}
            mods = compute_modified_paths(cand, before, after)
            self.assertEqual(mods, ("new_file.txt", "src/mod.py"))

    def test_validate_candidate_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "cand"
            canon = Path(td) / "canon"
            cand.mkdir()
            canon.mkdir()
            req = ExecutionRequest(
                execution_id="e1",
                project_id="p1",
                job_id="j1",
                candidate_root=cand,
                canonical_root=canon,
                mandate="m",
            )
            validate_candidate_isolation(req)

            # Candidate == Canonical fails
            bad_req = object.__new__(ExecutionRequest)
            object.__setattr__(bad_req, "candidate_root", cand)
            object.__setattr__(bad_req, "canonical_root", cand)
            with self.assertRaises(ValueError):
                validate_candidate_isolation(bad_req)


if __name__ == "__main__":
    unittest.main()
