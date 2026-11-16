"""Tests for the OpenHands executor adapter.

All lifecycle tests run against FakeOpenHandsRuntimeDriver — no Docker daemon,
network, or model access is required. The Docker driver is covered for pure
helpers and source invariants only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
from pathlib import Path
import secrets
import tempfile
import time
import unittest

from local_dev_mcp.executor_contract import (
    ExecutionRequest,
    ExecutorCheckpoint,
    ExecutorLifecycleState,
    ExecutorResourceLimits,
    NetworkMode,
    run_executor_conformance_suite_sync,
)
from local_dev_mcp.openhands_executor import (
    PINNED_OPENHANDS_IMAGE,
    DockerOpenHandsRuntimeDriver,
    OpenHandsAdapterConfig,
    OpenHandsExecutor,
    OpenHandsRuntimeDriver,
    build_openhands_container_argv,
    redact_openhands_diagnostics,
)


class FakeOpenHandsRuntimeDriver:
    """In-memory OpenHandsRuntimeDriver for deterministic tests.

    Simulates container start, health checks, candidate edits inside the
    mounted workspace, crashes, hangs (timeouts), and cancellation, while
    tracking active containers so tests can assert teardown guarantees.
    """

    def __init__(
        self,
        *,
        healthy: bool = True,
        fail_startup: bool = False,
        fail_health_wait: bool = False,
        crash: bool = False,
        hang: bool = False,
        edit_candidate: bool = True,
        driver_checkpoints: int = 0,
    ) -> None:
        self.healthy = healthy
        self.fail_startup = fail_startup
        self.fail_health_wait = fail_health_wait
        self.crash = crash
        self.hang = hang
        self.edit_candidate = edit_candidate
        self.driver_checkpoints = driver_checkpoints

        self.active_containers: dict[str, str] = {}
        self.start_calls: list[list[str]] = []
        self.teardown_calls: list[str] = []
        self.drive_calls: list[ExecutionRequest] = []
        self.session_keys: list[str] = []
        self.health_check_calls = 0
        self.wait_healthy_calls = 0

    async def check_runtime_health(
        self, config: OpenHandsAdapterConfig
    ) -> tuple[bool, str | None]:
        self.health_check_calls += 1
        if self.healthy:
            return True, None
        return False, "simulated unhealthy OpenHands runtime"

    async def start_container(
        self,
        config: OpenHandsAdapterConfig,
        argv: "list[str] | tuple[str, ...]",
        container_name: str,
    ) -> tuple[str, int]:
        self.start_calls.append(list(argv))
        if self.fail_startup:
            raise RuntimeError("simulated docker run failure")
        container_id = "fake-container-" + secrets.token_hex(4)
        self.active_containers[container_name] = container_id
        return container_id, 32768

    async def wait_healthy(
        self,
        host: str,
        port: int,
        session_api_key: str,
        timeout_seconds: float,
    ) -> bool:
        self.wait_healthy_calls += 1
        self.session_keys.append(session_api_key)
        return not self.fail_health_wait

    async def drive_action(
        self,
        host: str,
        port: int,
        session_api_key: str,
        request: ExecutionRequest,
        on_checkpoint=None,
    ) -> dict[str, object]:
        self.drive_calls.append(request)
        self.session_keys.append(session_api_key)
        if self.hang:
            await asyncio.sleep(3600)
            return {}
        if self.crash:
            raise RuntimeError("simulated OpenHands runtime crash")
        if self.edit_candidate:
            target = request.candidate_root / "openhands_edit.txt"
            target.write_text("edited by fake OpenHands runtime\n", encoding="utf-8")
        for index in range(self.driver_checkpoints):
            if on_checkpoint is not None:
                on_checkpoint(
                    ExecutorCheckpoint(
                        checkpoint_id=f"fake-chk-{index}",
                        execution_id=request.execution_id,
                        sequence=index + 2,
                        summary=f"fake runtime progress {index}",
                        created_at="2026-11-16T00:00:00Z",
                    )
                )
        return {"conversation_id": "fake-conv", "status": "completed", "exit_code": 0}

    async def teardown_container(
        self, config: OpenHandsAdapterConfig, container_name: str
    ) -> bool:
        self.teardown_calls.append(container_name)
        self.active_containers.pop(container_name, None)
        return True


async def _wait_until(predicate, timeout_seconds: float = 5.0) -> bool:
    """Poll ``predicate`` until it is True or the timeout elapses."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class TestOpenHandsAdapterConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        config = OpenHandsAdapterConfig()
        self.assertEqual(config.image, PINNED_OPENHANDS_IMAGE)
        self.assertEqual(config.docker_binary, "docker")
        self.assertEqual(config.loopback_host, "127.0.0.1")
        self.assertEqual(config.container_port, 8000)
        self.assertEqual(config.network_mode, NetworkMode.NONE)
        self.assertEqual(config.startup_timeout_seconds, 30.0)
        self.assertEqual(config.execution_timeout_seconds, 300.0)
        self.assertFalse(config.allow_unpinned_image)
        self.assertIsNone(config.host_uid)
        self.assertIsNone(config.host_gid)

    def test_unpinned_image_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            OpenHandsAdapterConfig(image="ghcr.io/openhands/agent-server:latest")
        self.assertIn("@sha256:", str(ctx.exception))

    def test_unpinned_image_allowed_when_opted_in(self) -> None:
        config = OpenHandsAdapterConfig(
            image="ghcr.io/openhands/agent-server:latest",
            allow_unpinned_image=True,
        )
        self.assertEqual(config.image, "ghcr.io/openhands/agent-server:latest")

    def test_pinned_image_accepted(self) -> None:
        config = OpenHandsAdapterConfig(image=PINNED_OPENHANDS_IMAGE)
        self.assertIn("@sha256:", config.image)

    def test_non_loopback_host_rejected(self) -> None:
        for bad_host in ("0.0.0.0", "10.0.0.1", "::1", "", "example.com"):
            with self.subTest(host=bad_host):
                with self.assertRaises(ValueError):
                    OpenHandsAdapterConfig(loopback_host=bad_host)

    def test_loopback_hosts_accepted(self) -> None:
        self.assertEqual(OpenHandsAdapterConfig(loopback_host="127.0.0.1").loopback_host, "127.0.0.1")
        self.assertEqual(OpenHandsAdapterConfig(loopback_host="localhost").loopback_host, "localhost")

    def test_non_positive_timeouts_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OpenHandsAdapterConfig(startup_timeout_seconds=0)
        with self.assertRaises(ValueError):
            OpenHandsAdapterConfig(execution_timeout_seconds=-1)


class TestBuildOpenhandsContainerArgv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.candidate = Path(self.tmpdir.name) / "candidate"
        self.candidate.mkdir()
        self.canonical = Path(self.tmpdir.name) / "canonical"
        self.canonical.mkdir()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _build(
        self,
        config: OpenHandsAdapterConfig | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        return build_openhands_container_argv(
            config or OpenHandsAdapterConfig(host_uid=1000, host_gid=1000),
            container_name="mcp-oh-test-container",
            candidate_root=self.candidate,
            session_api_key="sess-key-1",
            extra_env=extra_env,
        )

    def test_hardening_flags_present(self) -> None:
        argv = self._build()
        self.assertEqual(argv[0], "docker")
        self.assertIn("run", argv)
        self.assertIn("-d", argv)
        self.assertIn("--rm", argv)

        for token_pair in (
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges:true"),
            ("-p", "127.0.0.1:0:8000"),
            ("-u", "1000:1000"),
            ("-v", f"{self.candidate.resolve()}:/workspace:rw"),
            ("-w", "/workspace"),
            ("-e", "OH_SESSION_API_KEYS_0=sess-key-1"),
            ("-e", "OH_ENABLE_VNC=false"),
            ("--network", "none"),
            ("--name", "mcp-oh-test-container"),
        ):
            with self.subTest(flag=token_pair[0], value=token_pair[1]):
                positions = [i for i, x in enumerate(argv) if x == token_pair[0]]
                self.assertTrue(
                    any(i + 1 < len(argv) and argv[i + 1] == token_pair[1] for i in positions),
                    f"{token_pair} not found in argv: {argv}",
                )

        self.assertEqual(argv[-1], PINNED_OPENHANDS_IMAGE)

    def test_network_none_included_only_for_none_mode(self) -> None:
        argv = self._build(OpenHandsAdapterConfig(host_uid=1000, host_gid=1000))
        self.assertIn("--network", argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")

        loopback_config = OpenHandsAdapterConfig(
            host_uid=1000,
            host_gid=1000,
            network_mode=NetworkMode.LOOPBACK_ONLY,
        )
        argv_loopback = self._build(loopback_config)
        self.assertNotIn("--network", argv_loopback)

    def test_default_uid_gid_from_process(self) -> None:
        import os

        argv = self._build(OpenHandsAdapterConfig())
        expected_uid = os.getuid() if hasattr(os, "getuid") else 0
        expected_gid = os.getgid() if hasattr(os, "getgid") else 0
        self.assertIn(f"-u", argv)
        self.assertEqual(
            argv[argv.index("-u") + 1], f"{expected_uid}:{expected_gid}"
        )

    def test_extra_env_appended_before_image(self) -> None:
        argv = self._build(
            OpenHandsAdapterConfig(host_uid=1000, host_gid=1000),
            extra_env={"OH_EXTRA_FLAG": "1", "AAA_VAR": "2"},
        )
        self.assertIn("-e", argv)
        position = argv.index("-e", argv.index("OH_ENABLE_VNC=false"))
        self.assertEqual(argv[position + 1], "AAA_VAR=2")
        self.assertEqual(argv[position + 2], "-e")
        self.assertEqual(argv[position + 3], "OH_EXTRA_FLAG=1")
        self.assertEqual(argv[-1], PINNED_OPENHANDS_IMAGE)

    def test_never_mounts_canonical_root_or_credentials(self) -> None:
        argv = self._build()
        joined = "\n".join(argv)
        self.assertNotIn(str(self.canonical.resolve()), joined)
        self.assertNotIn("/home", joined)
        self.assertNotIn(".env", joined)
        self.assertNotIn("id_rsa", joined)
        # Only one volume mount: the candidate workspace.
        volume_positions = [i for i, token in enumerate(argv) if token == "-v"]
        self.assertEqual(len(volume_positions), 1)
        mount = argv[volume_positions[0] + 1]
        self.assertTrue(mount.startswith(str(self.candidate.resolve())))
        self.assertTrue(mount.endswith(":/workspace:rw"))

    def test_all_elements_are_plain_strings(self) -> None:
        argv = self._build()
        self.assertTrue(all(isinstance(token, str) for token in argv))
        self.assertTrue(all(token == token.strip() for token in argv if token.startswith("-")))


class TestRedactOpenhandsDiagnostics(unittest.TestCase):
    def test_scrubs_sensitive_keys(self) -> None:
        data = {
            "session_api_key": "super-secret",
            "env": {
                "OH_SESSION_API_KEYS_0": "super-secret",
                "OH_SESSION_API_KEYS_1": "other-secret",
                "HARMLESS": "visible",
            },
            "headers": {"X-Session-API-Key": "super-secret"},
            "lowercase_variant": {"x-session-api-key": "super-secret"},
            "note": "nothing to see",
        }
        redacted = redact_openhands_diagnostics(data, secrets=("super-secret", "other-secret"))
        self.assertEqual(redacted["session_api_key"], "<redacted>")
        self.assertEqual(redacted["env"]["OH_SESSION_API_KEYS_0"], "<redacted>")
        self.assertEqual(redacted["env"]["OH_SESSION_API_KEYS_1"], "<redacted>")
        self.assertEqual(redacted["env"]["HARMLESS"], "visible")
        self.assertEqual(redacted["headers"]["X-Session-API-Key"], "<redacted>")
        self.assertEqual(redacted["lowercase_variant"]["x-session-api-key"], "<redacted>")
        self.assertEqual(redacted["note"], "nothing to see")
        self.assertNotIn("super-secret", json.dumps(redacted))

    def test_scrubs_secret_values_in_lists_and_tuples(self) -> None:
        data = {
            "logs": ["token-A", "fine"],
            "nested": (("token-A", "fine"),),
            "deep": {"list": [{"session_api_key": "token-A"}]},
        }
        redacted = redact_openhands_diagnostics(data, secrets=("token-A",))
        self.assertEqual(redacted["logs"], ["<redacted>", "fine"])
        self.assertEqual(redacted["nested"], (("<redacted>", "fine"),))
        self.assertEqual(redacted["deep"]["list"][0]["session_api_key"], "<redacted>")

    def test_no_secrets_provided_only_key_redaction(self) -> None:
        data = {"session_api_key": "k", "plain": "k"}
        redacted = redact_openhands_diagnostics(data)
        self.assertEqual(redacted["session_api_key"], "<redacted>")
        self.assertEqual(redacted["plain"], "k")

    def test_original_data_is_not_mutated(self) -> None:
        data = {"session_api_key": "keep"}
        redacted = redact_openhands_diagnostics(data, secrets=("keep",))
        self.assertEqual(data["session_api_key"], "keep")
        self.assertEqual(redacted["session_api_key"], "<redacted>")


class TestOpenHandsExecutorLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)
        self.candidate = self.workspace / "candidate"
        self.canonical = self.workspace / "canonical"
        self.candidate.mkdir()
        self.canonical.mkdir()
        (self.canonical / "canary.txt").write_text(
            "canonical source of truth\n", encoding="utf-8"
        )

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    def _make_request(
        self,
        execution_id: str = "exec-test",
        network_mode: NetworkMode = NetworkMode.NONE,
    ) -> ExecutionRequest:
        return ExecutionRequest(
            execution_id=execution_id,
            project_id="test_project",
            job_id="test_job",
            candidate_root=self.candidate,
            canonical_root=self.canonical,
            mandate="Implement the assigned change",
            resource_limits=ExecutorResourceLimits(network_mode=network_mode),
        )

    async def test_start_completes_with_candidate_edit_and_redaction(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(driver_checkpoints=1)
        executor = OpenHandsExecutor(driver=driver)
        checkpoints: list[ExecutorCheckpoint] = []

        result = await executor.start(
            self._make_request("exec-ok"), on_checkpoint=checkpoints.append
        )

        self.assertEqual(result.state, ExecutorLifecycleState.COMPLETED)
        self.assertIsNone(result.error_code)
        self.assertFalse(result.grants_authority)
        self.assertEqual(result.adapter_id, "openhands")
        self.assertEqual(result.execution_id, "exec-ok")
        self.assertIn("openhands_edit.txt", result.modified_paths)
        self.assertTrue((self.candidate / "openhands_edit.txt").exists())
        self.assertEqual(
            (self.canonical / "canary.txt").read_text(encoding="utf-8"),
            "canonical source of truth\n",
        )
        self.assertFalse((self.canonical / "openhands_edit.txt").exists())

        # Checkpoints: executor launch checkpoint + driver progress checkpoint.
        self.assertGreaterEqual(len(checkpoints), 1)
        self.assertTrue(all(cp.execution_id == "exec-ok" for cp in checkpoints))
        self.assertGreaterEqual(len(result.checkpoints), 1)

        # Hardened argv actually handed to the driver.
        argv = driver.start_calls[0]
        self.assertIn("--cap-drop", argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertIn("--security-opt", argv)
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges:true")
        self.assertIn("127.0.0.1:0:8000", argv)
        self.assertEqual(argv[argv.index("--name") + 1][:8], "mcp-oh-e")
        self.assertEqual(argv[-1], PINNED_OPENHANDS_IMAGE)

        # Vendor diagnostics carry teardown evidence and zero key material.
        oh = result.vendor_diagnostics["openhands"]
        self.assertEqual(oh["teardown_verified"], True)
        self.assertEqual(oh["image"], PINNED_OPENHANDS_IMAGE)
        self.assertTrue(str(oh["container_name"]).startswith("mcp-oh-"))
        dump = json.dumps(
            {
                "diagnostics": dict(result.vendor_diagnostics),
                "checkpoints": [
                    dict(cp.diagnostics) for cp in result.checkpoints
                ],
            },
            default=str,
        )
        for key in driver.session_keys:
            self.assertNotIn(key, dump)

        # Teardown guarantee.
        self.assertEqual(len(driver.active_containers), 0)
        self.assertEqual(len(driver.teardown_calls), 1)
        self.assertEqual(result.containment_profile, "docker_hardened")
        self.assertEqual(result.resource_usage.exit_code, 0)

    async def test_start_without_on_checkpoint_callback(self) -> None:
        driver = FakeOpenHandsRuntimeDriver()
        executor = OpenHandsExecutor(driver=driver)
        result = await executor.start(self._make_request("exec-nocb"))
        self.assertEqual(result.state, ExecutorLifecycleState.COMPLETED)
        self.assertGreaterEqual(len(result.checkpoints), 1)

    async def test_crash_guarantees_teardown(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(crash=True)
        executor = OpenHandsExecutor(driver=driver)

        result = await executor.start(self._make_request("exec-crash"))

        self.assertEqual(result.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(result.error_code, "OPENHANDS_RUNTIME_CRASH")
        self.assertIn("simulated OpenHands runtime crash", str(result.error_message))
        self.assertFalse(result.grants_authority)
        self.assertEqual(len(driver.active_containers), 0)
        self.assertEqual(len(driver.teardown_calls), 1)
        self.assertTrue(result.vendor_diagnostics["openhands"]["teardown_verified"])
        self.assertGreaterEqual(len(result.checkpoints), 1)

    async def test_startup_timeout_guarantees_teardown(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(fail_health_wait=True)
        executor = OpenHandsExecutor(driver=driver)

        result = await executor.start(self._make_request("exec-starthealth"))

        self.assertEqual(result.state, ExecutorLifecycleState.TIMED_OUT)
        self.assertEqual(result.error_code, "OPENHANDS_STARTUP_TIMEOUT")
        self.assertEqual(len(driver.drive_calls), 0)
        self.assertEqual(len(driver.active_containers), 0)
        self.assertEqual(len(driver.teardown_calls), 1)

    async def test_execution_timeout_guarantees_teardown(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(hang=True)
        executor = OpenHandsExecutor(
            config=OpenHandsAdapterConfig(execution_timeout_seconds=0.2),
            driver=driver,
        )

        result = await executor.start(self._make_request("exec-hang"))

        self.assertEqual(result.state, ExecutorLifecycleState.TIMED_OUT)
        self.assertEqual(result.error_code, "OPENHANDS_EXECUTION_TIMEOUT")
        self.assertFalse(result.grants_authority)
        self.assertEqual(len(driver.active_containers), 0)
        self.assertEqual(len(driver.teardown_calls), 1)

    async def test_start_failure_guarantees_teardown(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(fail_startup=True)
        executor = OpenHandsExecutor(driver=driver)

        result = await executor.start(self._make_request("exec-startfail"))

        self.assertEqual(result.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(result.error_code, "OPENHANDS_CONTAINER_START_FAILED")
        self.assertEqual(len(driver.drive_calls), 0)
        self.assertEqual(len(driver.active_containers), 0)
        self.assertEqual(len(driver.teardown_calls), 1)
        self.assertGreaterEqual(len(result.checkpoints), 1)

    async def test_cancel_guarantees_teardown(self) -> None:
        driver = FakeOpenHandsRuntimeDriver(hang=True)
        executor = OpenHandsExecutor(driver=driver)
        request = self._make_request("exec-cancel")

        task = asyncio.ensure_future(executor.start(request))
        self.assertTrue(
            await _wait_until(lambda: len(driver.drive_calls) > 0),
            "drive_action was never reached",
        )
        self.assertEqual(len(driver.active_containers), 1)

        cancelled = await executor.cancel(request.execution_id, reason="test_cancel")
        self.assertTrue(cancelled)

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(len(driver.active_containers), 0)
        self.assertNotIn(request.execution_id, executor._active_containers)
        self.assertNotIn(request.execution_id, executor._active_tasks)

    async def test_cancel_unknown_execution_returns_true(self) -> None:
        driver = FakeOpenHandsRuntimeDriver()
        executor = OpenHandsExecutor(driver=driver)
        self.assertTrue(await executor.cancel("never-started"))

    async def test_resume_unsupported(self) -> None:
        driver = FakeOpenHandsRuntimeDriver()
        executor = OpenHandsExecutor(driver=driver)
        checkpoint = ExecutorCheckpoint(
            checkpoint_id="chk-dummy",
            execution_id="exec-resume",
            sequence=1,
            summary="dummy",
            created_at="2026-11-16T00:00:00Z",
        )

        result = await executor.resume(
            self._make_request("exec-resume"), checkpoint
        )

        self.assertEqual(result.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(result.error_code, "OPENHANDS_RESUME_UNSUPPORTED")
        self.assertIn("does not support resume", result.summary)
        self.assertFalse(result.grants_authority)
        self.assertEqual(len(driver.active_containers), 0)

    async def test_capability_mismatch_rejects_before_launch(self) -> None:
        driver = FakeOpenHandsRuntimeDriver()
        executor = OpenHandsExecutor(driver=driver)

        result = await executor.start(
            self._make_request("exec-net", network_mode=NetworkMode.ALLOWLIST)
        )

        self.assertEqual(result.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(result.error_code, "OPENHANDS_CAPABILITY_MISMATCH")
        self.assertEqual(len(driver.start_calls), 0)
        self.assertEqual(len(driver.active_containers), 0)

    async def test_health_reports_driver_status(self) -> None:
        driver = FakeOpenHandsRuntimeDriver()
        executor = OpenHandsExecutor(driver=driver)
        health = await executor.health()
        self.assertTrue(health.healthy)
        self.assertEqual(health.adapter_id, "openhands")
        self.assertTrue(health.containment_available)
        self.assertEqual(health.details["image"], PINNED_OPENHANDS_IMAGE)
        self.assertEqual(health.details["network_mode"], "none")

        unhealthy_executor = OpenHandsExecutor(
            driver=FakeOpenHandsRuntimeDriver(healthy=False)
        )
        unhealthy = await unhealthy_executor.health()
        self.assertFalse(unhealthy.healthy)
        self.assertFalse(unhealthy.containment_available)
        self.assertIn("unhealthy", str(unhealthy.reason))

    async def test_capabilities_declaration(self) -> None:
        executor = OpenHandsExecutor(driver=FakeOpenHandsRuntimeDriver())
        caps = executor.capabilities()
        self.assertEqual(caps.adapter_id, "openhands")
        self.assertEqual(caps.adapter_version, "1.0.0")
        self.assertFalse(caps.supports_resume)
        self.assertFalse(caps.supports_streaming)
        self.assertTrue(caps.supports_cancellation)
        self.assertTrue(caps.supports_checkpoints)
        self.assertEqual(
            caps.supported_network_modes, (NetworkMode.NONE, NetworkMode.LOOPBACK_ONLY)
        )
        self.assertTrue(caps.requires_container_runtime)
        self.assertTrue(caps.supports_host_containment)
        self.assertEqual(caps.declared_tools, ("Read", "Edit", "Write", "Glob", "Grep"))


class TestOpenHandsConformance(unittest.TestCase):
    def test_conformance_suite_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_executor_conformance_suite_sync(
                lambda: OpenHandsExecutor(driver=FakeOpenHandsRuntimeDriver()),
                Path(tmpdir),
            )
        self.assertTrue(
            report.passed, f"Conformance failed: {report.failures}"
        )
        self.assertEqual(report.adapter_id, "openhands")
        self.assertEqual(report.failures, ())


class TestDockerDriverPureHelpers(unittest.TestCase):
    def test_parse_mapped_port_variants(self) -> None:
        from local_dev_mcp.openhands_executor import _parse_mapped_port

        self.assertEqual(
            _parse_mapped_port("8000/tcp -> 127.0.0.1:32768"), 32768
        )
        self.assertEqual(_parse_mapped_port("0.0.0.0:49153"), 49153)
        self.assertIsNone(_parse_mapped_port("no ports here"))
        self.assertIsNone(_parse_mapped_port(""))

    def test_docker_driver_implements_protocol(self) -> None:
        driver = DockerOpenHandsRuntimeDriver()
        self.assertIsInstance(driver, OpenHandsRuntimeDriver)


class TestSourceInvariants(unittest.TestCase):
    def test_no_shell_true_and_no_fastmcp(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "local_dev_mcp"
            / "openhands_executor.py"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("shell= True", source)
        self.assertNotIn("fastmcp", source.lower())
        self.assertIn("create_subprocess_exec", source)


if __name__ == "__main__":
    unittest.main()
