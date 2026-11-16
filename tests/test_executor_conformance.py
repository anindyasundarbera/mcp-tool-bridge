"""Unit tests for the executor contract, lifecycle transitions, and conformance suite."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from types import MappingProxyType
import unittest

from local_dev_mcp.executor_contract import (
    TERMINAL_EXECUTOR_STATES,
    VALID_EXECUTOR_TRANSITIONS,
    ConformanceCheckResult,
    DeterministicFakeExecutor,
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCertificationReport,
    ExecutorCheckpoint,
    ExecutorHealthStatus,
    ExecutorLifecycleState,
    ExecutorPort,
    ExecutorRegistry,
    ExecutorResourceLimits,
    ExecutorResourceUsage,
    NetworkMode,
    run_executor_conformance_suite,
    run_executor_conformance_suite_sync,
    validate_executor_transition,
)


class TestExecutorLifecycleAndStates(unittest.TestCase):
    def test_lifecycle_state_values(self) -> None:
        self.assertEqual(ExecutorLifecycleState.CREATED, "created")
        self.assertEqual(ExecutorLifecycleState.STARTING, "starting")
        self.assertEqual(ExecutorLifecycleState.RUNNING, "running")
        self.assertEqual(ExecutorLifecycleState.PAUSED, "paused")
        self.assertEqual(ExecutorLifecycleState.CANCELLING, "cancelling")
        self.assertEqual(ExecutorLifecycleState.COMPLETED, "completed")
        self.assertEqual(ExecutorLifecycleState.FAILED, "failed")
        self.assertEqual(ExecutorLifecycleState.CANCELLED, "cancelled")
        self.assertEqual(ExecutorLifecycleState.TIMED_OUT, "timed_out")

    def test_terminal_states_set(self) -> None:
        self.assertEqual(
            TERMINAL_EXECUTOR_STATES,
            frozenset(
                {
                    ExecutorLifecycleState.COMPLETED,
                    ExecutorLifecycleState.FAILED,
                    ExecutorLifecycleState.CANCELLED,
                    ExecutorLifecycleState.TIMED_OUT,
                }
            ),
        )

    def test_valid_transitions(self) -> None:
        valid_pairs = [
            (ExecutorLifecycleState.CREATED, ExecutorLifecycleState.STARTING),
            (ExecutorLifecycleState.CREATED, ExecutorLifecycleState.CANCELLED),
            (ExecutorLifecycleState.CREATED, ExecutorLifecycleState.FAILED),
            (ExecutorLifecycleState.STARTING, ExecutorLifecycleState.RUNNING),
            (ExecutorLifecycleState.STARTING, ExecutorLifecycleState.CANCELLING),
            (ExecutorLifecycleState.STARTING, ExecutorLifecycleState.CANCELLED),
            (ExecutorLifecycleState.STARTING, ExecutorLifecycleState.TIMED_OUT),
            (ExecutorLifecycleState.RUNNING, ExecutorLifecycleState.PAUSED),
            (ExecutorLifecycleState.RUNNING, ExecutorLifecycleState.CANCELLING),
            (ExecutorLifecycleState.RUNNING, ExecutorLifecycleState.COMPLETED),
            (ExecutorLifecycleState.RUNNING, ExecutorLifecycleState.FAILED),
            (ExecutorLifecycleState.RUNNING, ExecutorLifecycleState.TIMED_OUT),
            (ExecutorLifecycleState.PAUSED, ExecutorLifecycleState.RUNNING),
            (ExecutorLifecycleState.PAUSED, ExecutorLifecycleState.CANCELLING),
            (ExecutorLifecycleState.CANCELLING, ExecutorLifecycleState.CANCELLED),
            (ExecutorLifecycleState.CANCELLING, ExecutorLifecycleState.FAILED),
            (ExecutorLifecycleState.CANCELLING, ExecutorLifecycleState.TIMED_OUT),
            (ExecutorLifecycleState.CANCELLING, ExecutorLifecycleState.COMPLETED),
        ]
        for src, dst in valid_pairs:
            with self.subTest(src=src, dst=dst):
                validate_executor_transition(src, dst)
                validate_executor_transition(src.value, dst.value)

    def test_invalid_transitions(self) -> None:
        invalid_pairs = [
            (ExecutorLifecycleState.COMPLETED, ExecutorLifecycleState.RUNNING),
            (ExecutorLifecycleState.FAILED, ExecutorLifecycleState.STARTING),
            (ExecutorLifecycleState.CANCELLED, ExecutorLifecycleState.RUNNING),
            (ExecutorLifecycleState.TIMED_OUT, ExecutorLifecycleState.STARTING),
            (ExecutorLifecycleState.CREATED, ExecutorLifecycleState.COMPLETED),
            (ExecutorLifecycleState.PAUSED, ExecutorLifecycleState.COMPLETED),
        ]
        for src, dst in invalid_pairs:
            with self.subTest(src=src, dst=dst):
                with self.assertRaises(ValueError):
                    validate_executor_transition(src, dst)

    def test_validate_unknown_state_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_executor_transition("invalid_state", ExecutorLifecycleState.RUNNING)


class TestNetworkModeAndCapabilities(unittest.TestCase):
    def test_network_mode_values(self) -> None:
        self.assertEqual(NetworkMode.NONE, "none")
        self.assertEqual(NetworkMode.LOOPBACK_ONLY, "loopback_only")
        self.assertEqual(NetworkMode.ALLOWLIST, "allowlist")

    def test_executor_capabilities_valid(self) -> None:
        caps = ExecutorCapabilities(
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            supports_resume=True,
            supported_network_modes=(NetworkMode.NONE, NetworkMode.LOOPBACK_ONLY),
            declared_tools=("Read", "Write"),
            max_concurrent_executions=2,
        )
        self.assertEqual(caps.adapter_id, "test_adapter")
        self.assertEqual(caps.max_concurrent_executions, 2)
        self.assertEqual(caps.supported_network_modes, (NetworkMode.NONE, NetworkMode.LOOPBACK_ONLY))
        self.assertEqual(caps.declared_tools, ("Read", "Write"))

    def test_executor_capabilities_validation_failures(self) -> None:
        with self.assertRaises(ValueError):
            ExecutorCapabilities(adapter_id="", adapter_version="1.0.0")

        with self.assertRaises(ValueError):
            ExecutorCapabilities(adapter_id="ok", adapter_version="")

        with self.assertRaises(ValueError):
            ExecutorCapabilities(adapter_id="ok", adapter_version="1.0.0", max_concurrent_executions=0)

        with self.assertRaises(ValueError):
            ExecutorCapabilities(adapter_id="ok", adapter_version="1.0.0", supported_network_modes=())


class TestResourceLimitsAndRequests(unittest.TestCase):
    def test_resource_limits_valid(self) -> None:
        limits = ExecutorResourceLimits(
            timeout_seconds=60.0,
            max_turns=10,
            max_output_bytes=1000,
            max_memory_mb=512,
            max_pids=64,
            network_mode=NetworkMode.NONE,
        )
        self.assertEqual(limits.timeout_seconds, 60.0)
        self.assertEqual(limits.max_turns, 10)
        self.assertEqual(limits.max_output_bytes, 1000)

    def test_resource_limits_validation_failures(self) -> None:
        with self.assertRaises(ValueError):
            ExecutorResourceLimits(timeout_seconds=0.0)
        with self.assertRaises(ValueError):
            ExecutorResourceLimits(max_turns=0)
        with self.assertRaises(ValueError):
            ExecutorResourceLimits(max_output_bytes=0)
        with self.assertRaises(ValueError):
            ExecutorResourceLimits(max_memory_mb=0)
        with self.assertRaises(ValueError):
            ExecutorResourceLimits(max_pids=0)

    def test_execution_request_valid_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = Path(tmpdir) / "candidate"
            canon = Path(tmpdir) / "canonical"
            cand.mkdir()
            canon.mkdir()

            req = ExecutionRequest(
                execution_id="ex1",
                project_id="proj1",
                job_id="job1",
                candidate_root=cand,
                canonical_root=canon,
                mandate="Mandate text",
                env_projection={"A": "1"},
                metadata={"meta": "val"},
            )
            self.assertEqual(req.candidate_root, cand)
            self.assertEqual(req.canonical_root, canon)
            self.assertIsInstance(req.env_projection, MappingProxyType)
            self.assertIsInstance(req.metadata, MappingProxyType)

            with self.assertRaises(TypeError):
                req.env_projection["B"] = "2"  # type: ignore

    def test_execution_request_validation_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(ValueError):
                # candidate_root == canonical_root
                ExecutionRequest(
                    execution_id="ex1",
                    project_id="p1",
                    job_id="j1",
                    candidate_root=root,
                    canonical_root=root,
                    mandate="mandate",
                )

            with self.assertRaises(ValueError):
                # relative candidate_root
                ExecutionRequest(
                    execution_id="ex1",
                    project_id="p1",
                    job_id="j1",
                    candidate_root=Path("relative/path"),
                    mandate="mandate",
                )

            with self.assertRaises(ValueError):
                # empty mandate
                ExecutionRequest(
                    execution_id="ex1",
                    project_id="p1",
                    job_id="j1",
                    candidate_root=root,
                    mandate="",
                )


class TestExecutionResultAndCheckpoints(unittest.TestCase):
    def test_checkpoint_valid_and_immutable(self) -> None:
        cp = ExecutorCheckpoint(
            checkpoint_id="chk1",
            execution_id="ex1",
            sequence=0,
            summary="Init",
            created_at="2026-11-16T00:00:00Z",
            diagnostics={"k": "v"},
        )
        self.assertEqual(cp.sequence, 0)
        self.assertIsInstance(cp.diagnostics, MappingProxyType)
        with self.assertRaises(TypeError):
            cp.diagnostics["k2"] = "v2"  # type: ignore

    def test_checkpoint_validation_failures(self) -> None:
        with self.assertRaises(ValueError):
            ExecutorCheckpoint(
                checkpoint_id="",
                execution_id="ex1",
                sequence=0,
                summary="Init",
                created_at="now",
            )
        with self.assertRaises(ValueError):
            ExecutorCheckpoint(
                checkpoint_id="chk1",
                execution_id="ex1",
                sequence=-1,
                summary="Init",
                created_at="now",
            )

    def test_execution_result_grants_authority_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.COMPLETED,
                summary="done",
                grants_authority=True,
            )
        self.assertIn("grants_authority must be False", str(ctx.exception))

    def test_execution_result_completed_with_error_code_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.COMPLETED,
                summary="done",
                error_code="ERR",
            )
        self.assertIn("must not have an error_code", str(ctx.exception))

    def test_execution_result_failed_without_error_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.FAILED,
                summary="failed",
            )
        self.assertIn("must specify error_code or error_message", str(ctx.exception))

    def test_execution_result_timed_out_without_error_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.TIMED_OUT,
                summary="timed out",
            )
        self.assertIn("must specify error_code or error_message", str(ctx.exception))

    def test_execution_result_modified_paths_validation(self) -> None:
        # Valid paths
        res = ExecutionResult(
            execution_id="ex1",
            adapter_id="test",
            state=ExecutorLifecycleState.COMPLETED,
            summary="ok",
            modified_paths=("src/foo.py", "bar.txt"),
        )
        self.assertEqual(res.modified_paths, ("src/foo.py", "bar.txt"))

        # Rejects absolute paths
        with self.assertRaises(ValueError):
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.COMPLETED,
                summary="ok",
                modified_paths=("/etc/passwd",),
            )

        # Rejects traversal paths
        with self.assertRaises(ValueError):
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.COMPLETED,
                summary="ok",
                modified_paths=("a/../b.txt",),
            )

        # Rejects empty paths
        with self.assertRaises(ValueError):
            ExecutionResult(
                execution_id="ex1",
                adapter_id="test",
                state=ExecutorLifecycleState.COMPLETED,
                summary="ok",
                modified_paths=("",),
            )


class TestExecutorRegistry(unittest.TestCase):
    def test_registry_registration_and_lookup(self) -> None:
        registry = ExecutorRegistry()
        executor = DeterministicFakeExecutor(adapter_id="fake_1")
        registry.register(executor)

        self.assertTrue(registry.has("fake_1"))
        self.assertFalse(registry.has("fake_2"))
        self.assertEqual(registry.get("fake_1"), executor)
        self.assertEqual(len(registry.list_adapters()), 1)

        # Duplicate registration rejected
        with self.assertRaises(ValueError):
            registry.register(executor)

        # Replace registration works
        new_executor = DeterministicFakeExecutor(adapter_id="fake_1", adapter_version="2.0.0")
        registry.register(new_executor, replace=True)
        self.assertEqual(registry.get("fake_1").capabilities().adapter_version, "2.0.0")

        # Missing lookup raises KeyError
        with self.assertRaises(KeyError):
            registry.get("non_existent")


class TestDeterministicFakeExecutorFaultModes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)
        self.candidate = self.workspace / "candidate"
        self.canonical = self.workspace / "canonical"
        self.candidate.mkdir()
        self.canonical.mkdir()

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    def _make_req(self, execution_id: str = "ex_test", net_mode: NetworkMode = NetworkMode.NONE) -> ExecutionRequest:
        return ExecutionRequest(
            execution_id=execution_id,
            project_id="test_proj",
            job_id="test_job",
            candidate_root=self.candidate,
            canonical_root=self.canonical,
            mandate="test",
            resource_limits=ExecutorResourceLimits(network_mode=net_mode),
        )

    async def test_normal_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="normal")
        checkpoints: list[ExecutorCheckpoint] = []
        req = self._make_req("ex_norm")

        res = await executor.start(req, on_checkpoint=checkpoints.append)
        self.assertEqual(res.state, ExecutorLifecycleState.COMPLETED)
        self.assertEqual(res.modified_paths, ("fake_output.txt",))
        self.assertTrue((self.candidate / "fake_output.txt").exists())
        self.assertEqual(len(checkpoints), 1)

        # Test resume
        resumed = await executor.resume(req, checkpoints[0])
        self.assertEqual(resumed.state, ExecutorLifecycleState.COMPLETED)

    async def test_pause_and_resume_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="pause_and_resume")
        checkpoints: list[ExecutorCheckpoint] = []
        req = self._make_req("ex_pause")

        res = await executor.start(req, on_checkpoint=checkpoints.append)
        self.assertEqual(res.state, ExecutorLifecycleState.PAUSED)
        self.assertEqual(len(checkpoints), 1)

        resumed = await executor.resume(req, checkpoints[0])
        self.assertEqual(resumed.state, ExecutorLifecycleState.COMPLETED)

    async def test_timeout_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="timeout")
        res = await executor.start(self._make_req("ex_timeout"))
        self.assertEqual(res.state, ExecutorLifecycleState.TIMED_OUT)
        self.assertEqual(res.error_code, "EXECUTION_TIMEOUT")

    async def test_crash_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="crash")
        res = await executor.start(self._make_req("ex_crash"))
        self.assertEqual(res.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(res.error_code, "EXECUTOR_CRASH")

    async def test_no_progress_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="no_progress")
        res = await executor.start(self._make_req("ex_no_prog"))
        self.assertEqual(res.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(res.error_code, "NO_PROGRESS")

    async def test_malformed_result_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="malformed_result")
        res = await executor.start(self._make_req("ex_malform"))
        # Malformed result has COMPLETED with an error_code
        self.assertEqual(res.state, ExecutorLifecycleState.COMPLETED)
        self.assertIsNotNone(res.error_code)

    async def test_cancel_race_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="cancel_race")
        res = await executor.start(self._make_req("ex_race"))
        self.assertEqual(res.state, ExecutorLifecycleState.CANCELLED)
        self.assertIn("ex_race", executor.cancelled_executions)

    async def test_health_failure_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="health_failure")
        health = await executor.health()
        self.assertFalse(health.healthy)
        self.assertIn("simulated outage", str(health.reason))

    async def test_capability_mismatch_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="capability_mismatch")
        # Default request has NetworkMode.NONE, adapter only declares ALLOWLIST
        res = await executor.start(self._make_req("ex_mismatch", net_mode=NetworkMode.NONE))
        self.assertEqual(res.state, ExecutorLifecycleState.FAILED)
        self.assertEqual(res.error_code, "CAPABILITY_MISMATCH")

    async def test_canonical_write_attempt_mode(self) -> None:
        executor = DeterministicFakeExecutor(fault_mode="canonical_write_attempt")
        await executor.start(self._make_req("ex_breach"))
        self.assertTrue((self.canonical / "unauthorized_breach.txt").exists())


class TestConformanceSuite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name)

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_conformance_passes_for_normal_fake(self) -> None:
        report = await run_executor_conformance_suite(
            lambda: DeterministicFakeExecutor("normal"),
            self.workspace,
        )
        self.assertTrue(report.passed, f"Conformance failed: {report.failures}")
        self.assertEqual(report.failures, ())
        self.assertGreater(len(report.checks), 0)

    def test_sync_conformance_wrapper_passes(self) -> None:
        report = run_executor_conformance_suite_sync(
            lambda: DeterministicFakeExecutor("normal"),
            self.workspace,
        )
        self.assertTrue(report.passed)
        self.assertEqual(report.failures, ())

    async def test_conformance_fails_for_unhealthy_adapter(self) -> None:
        report = await run_executor_conformance_suite(
            lambda: DeterministicFakeExecutor("health_failure"),
            self.workspace,
        )
        self.assertFalse(report.passed)
        self.assertTrue(any("health_check" in f for f in report.failures))

    async def test_conformance_fails_for_canonical_root_mutation(self) -> None:
        report = await run_executor_conformance_suite(
            lambda: DeterministicFakeExecutor("canonical_write_attempt"),
            self.workspace,
        )
        self.assertFalse(report.passed)
        self.assertTrue(any("canonical_immutability" in f for f in report.failures))

    async def test_conformance_fails_for_broken_cancel(self) -> None:
        class BrokenCancelExecutor(DeterministicFakeExecutor):
            async def cancel(self, execution_id: str, reason: str = "cancelled_by_caller") -> bool:
                raise RuntimeError("Exploding cancel")

        report = await run_executor_conformance_suite(BrokenCancelExecutor, self.workspace)
        self.assertFalse(report.passed)
        self.assertTrue(any("cancellation_safety" in f for f in report.failures))

    async def test_conformance_fails_for_mismatched_execution_id(self) -> None:
        class BrokenExecutionIdExecutor(DeterministicFakeExecutor):
            async def start(
                self,
                request: ExecutionRequest,
                on_checkpoint: Any = None,
            ) -> ExecutionResult:
                res = await super().start(request, on_checkpoint)
                return ExecutionResult(
                    execution_id="wrong_id",
                    adapter_id=res.adapter_id,
                    state=res.state,
                    summary=res.summary,
                    modified_paths=res.modified_paths,
                )

        report = await run_executor_conformance_suite(BrokenExecutionIdExecutor, self.workspace)
        self.assertFalse(report.passed)
        self.assertTrue(any("execution_lifecycle" in f for f in report.failures))

    async def test_conformance_fails_for_non_resumable_claiming_completed(self) -> None:
        class FakeNonResumableLying(DeterministicFakeExecutor):
            def __init__(self) -> None:
                super().__init__(supports_resume=False)

            async def resume(
                self,
                request: ExecutionRequest,
                checkpoint: ExecutorCheckpoint,
                on_checkpoint: Any = None,
            ) -> ExecutionResult:
                return ExecutionResult(
                    execution_id=request.execution_id,
                    adapter_id=self.adapter_id,
                    state=ExecutorLifecycleState.COMPLETED,
                    summary="Lied about completion",
                )

        report = await run_executor_conformance_suite(FakeNonResumableLying, self.workspace)
        self.assertFalse(report.passed)
        self.assertTrue(any("resumption_semantics" in f for f in report.failures))


if __name__ == "__main__":
    unittest.main()
