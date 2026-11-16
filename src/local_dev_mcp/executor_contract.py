"""Executor contract, lifecycle types, and conformance test runner for MCP Tool Bridge.

Phase 2 (M2.1) introduces this module to decouple executor implementations (e.g. Claude
Code CLI, Docker sandbox, future microVM/gVisor backends) from the core bridge lifecycle.
Executors must adhere to the ExecutorPort protocol, uphold invariant security gates,
and achieve certification through the conformance suite before production enablement.

Zero FastMCP imports; stdlib only.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass, field
from enum import Enum
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
import uuid


class ExecutorLifecycleState(str, Enum):
    """Lifecycle states for an executor process or container session."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_EXECUTOR_STATES: frozenset[ExecutorLifecycleState] = frozenset(
    {
        ExecutorLifecycleState.COMPLETED,
        ExecutorLifecycleState.FAILED,
        ExecutorLifecycleState.CANCELLED,
        ExecutorLifecycleState.TIMED_OUT,
    }
)


VALID_EXECUTOR_TRANSITIONS: dict[ExecutorLifecycleState, frozenset[ExecutorLifecycleState]] = {
    ExecutorLifecycleState.CREATED: frozenset(
        {
            ExecutorLifecycleState.STARTING,
            ExecutorLifecycleState.CANCELLED,
            ExecutorLifecycleState.FAILED,
        }
    ),
    ExecutorLifecycleState.STARTING: frozenset(
        {
            ExecutorLifecycleState.RUNNING,
            ExecutorLifecycleState.CANCELLING,
            ExecutorLifecycleState.FAILED,
            ExecutorLifecycleState.CANCELLED,
            ExecutorLifecycleState.TIMED_OUT,
        }
    ),
    ExecutorLifecycleState.RUNNING: frozenset(
        {
            ExecutorLifecycleState.PAUSED,
            ExecutorLifecycleState.CANCELLING,
            ExecutorLifecycleState.COMPLETED,
            ExecutorLifecycleState.FAILED,
            ExecutorLifecycleState.CANCELLED,
            ExecutorLifecycleState.TIMED_OUT,
        }
    ),
    ExecutorLifecycleState.PAUSED: frozenset(
        {
            ExecutorLifecycleState.RUNNING,
            ExecutorLifecycleState.CANCELLING,
            ExecutorLifecycleState.CANCELLED,
            ExecutorLifecycleState.FAILED,
            ExecutorLifecycleState.TIMED_OUT,
        }
    ),
    ExecutorLifecycleState.CANCELLING: frozenset(
        {
            ExecutorLifecycleState.CANCELLED,
            ExecutorLifecycleState.FAILED,
            ExecutorLifecycleState.TIMED_OUT,
            ExecutorLifecycleState.COMPLETED,
        }
    ),
    ExecutorLifecycleState.COMPLETED: frozenset(),
    ExecutorLifecycleState.FAILED: frozenset(),
    ExecutorLifecycleState.CANCELLED: frozenset(),
    ExecutorLifecycleState.TIMED_OUT: frozenset(),
}


def validate_executor_transition(
    current: ExecutorLifecycleState | str,
    target: ExecutorLifecycleState | str,
) -> None:
    """Validate that transitioning from ``current`` to ``target`` is allowed.

    Raises:
        ValueError: If transition is illegal or either state is unknown.
    """
    curr = ExecutorLifecycleState(current) if isinstance(current, str) else current
    tgt = ExecutorLifecycleState(target) if isinstance(target, str) else target

    allowed = VALID_EXECUTOR_TRANSITIONS.get(curr, frozenset())
    if tgt not in allowed:
        raise ValueError(
            f"Invalid executor lifecycle transition from {curr.value} to {tgt.value}; "
            f"allowed transitions: {sorted([s.value for s in allowed])}"
        )


class NetworkMode(str, Enum):
    """Network containment modes supported by executors."""

    NONE = "none"
    LOOPBACK_ONLY = "loopback_only"
    ALLOWLIST = "allowlist"


@dataclass(frozen=True, slots=True)
class ExecutorCapabilities:
    """Declared capabilities and boundaries of an executor adapter."""

    adapter_id: str
    adapter_version: str
    supports_resume: bool = False
    supports_streaming: bool = False
    supports_cancellation: bool = True
    supports_checkpoints: bool = True
    supported_network_modes: tuple[NetworkMode, ...] = (NetworkMode.NONE,)
    requires_container_runtime: bool = False
    supports_host_containment: bool = True
    declared_tools: tuple[str, ...] = ()
    max_concurrent_executions: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not self.adapter_id.strip():
            raise ValueError("adapter_id must be a non-empty string")
        if not isinstance(self.adapter_version, str) or not self.adapter_version.strip():
            raise ValueError("adapter_version must be a non-empty string")
        if (
            isinstance(self.max_concurrent_executions, bool)
            or not isinstance(self.max_concurrent_executions, int)
            or self.max_concurrent_executions < 1
        ):
            raise ValueError("max_concurrent_executions must be >= 1")
        if not self.supported_network_modes or len(self.supported_network_modes) < 1:
            raise ValueError("supported_network_modes must not be empty")

        modes = tuple(
            NetworkMode(m) if isinstance(m, str) else m for m in self.supported_network_modes
        )
        object.__setattr__(self, "supported_network_modes", modes)
        object.__setattr__(self, "declared_tools", tuple(self.declared_tools))


@dataclass(frozen=True, slots=True)
class ExecutorResourceLimits:
    """Enforced resource constraints for an execution."""

    timeout_seconds: float = 300.0
    max_turns: int = 16
    max_output_bytes: int = 262_144
    max_memory_mb: int | None = None
    max_pids: int | None = None
    network_mode: NetworkMode = NetworkMode.NONE

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(self.max_turns, bool)
            or not isinstance(self.max_turns, int)
            or self.max_turns < 1
        ):
            raise ValueError("max_turns must be >= 1")
        if (
            isinstance(self.max_output_bytes, bool)
            or not isinstance(self.max_output_bytes, int)
            or self.max_output_bytes < 1
        ):
            raise ValueError("max_output_bytes must be >= 1")
        if self.max_memory_mb is not None and (
            isinstance(self.max_memory_mb, bool)
            or not isinstance(self.max_memory_mb, int)
            or self.max_memory_mb < 1
        ):
            raise ValueError("max_memory_mb must be >= 1")
        if self.max_pids is not None and (
            isinstance(self.max_pids, bool)
            or not isinstance(self.max_pids, int)
            or self.max_pids < 1
        ):
            raise ValueError("max_pids must be >= 1")
        if isinstance(self.network_mode, str):
            object.__setattr__(self, "network_mode", NetworkMode(self.network_mode))


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Canonical invocation request dispatched to an executor."""

    execution_id: str
    project_id: str
    job_id: str
    candidate_root: Path
    mandate: str
    canonical_root: Path | None = None
    allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep")
    denied_tools: tuple[str, ...] = ("Bash", "WebFetch", "WebSearch")
    env_projection: Mapping[str, str] = field(default_factory=dict)
    resource_limits: ExecutorResourceLimits = field(default_factory=ExecutorResourceLimits)
    resume_checkpoint_id: str | None = None
    resume_session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        if not isinstance(self.project_id, str) or not self.project_id.strip():
            raise ValueError("project_id must be a non-empty string")
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if not isinstance(self.mandate, str) or not self.mandate.strip():
            raise ValueError("mandate must be a non-empty string")

        cand_path = Path(self.candidate_root)
        if not cand_path.is_absolute():
            raise ValueError(f"candidate_root must be an absolute path, got {self.candidate_root!r}")
        object.__setattr__(self, "candidate_root", cand_path)

        if self.canonical_root is not None:
            canon_path = Path(self.canonical_root)
            if cand_path.resolve() == canon_path.resolve():
                raise ValueError("candidate_root and canonical_root must not resolve to the same path")
            object.__setattr__(self, "canonical_root", canon_path)

        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "denied_tools", tuple(self.denied_tools))
        object.__setattr__(self, "env_projection", MappingProxyType(dict(self.env_projection)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ExecutorCheckpoint:
    """Intermediate progress checkpoint captured during execution."""

    checkpoint_id: str
    execution_id: str
    sequence: int
    summary: str
    created_at: str
    resumable_token: str | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.checkpoint_id, str) or not self.checkpoint_id.strip():
            raise ValueError("checkpoint_id must be a non-empty string")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise ValueError("execution_id must be a non-empty string")
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))


@dataclass(frozen=True, slots=True)
class ExecutorResourceUsage:
    """Resource consumption metrics measured for an execution."""

    duration_ms: int = 0
    turns_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    exit_code: int | None = None
    peak_memory_mb: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be non-negative")
        if (
            isinstance(self.turns_used, bool)
            or not isinstance(self.turns_used, int)
            or self.turns_used < 0
        ):
            raise ValueError("turns_used must be non-negative")
        if (
            isinstance(self.input_tokens, bool)
            or not isinstance(self.input_tokens, int)
            or self.input_tokens < 0
        ):
            raise ValueError("input_tokens must be non-negative")
        if (
            isinstance(self.output_tokens, bool)
            or not isinstance(self.output_tokens, int)
            or self.output_tokens < 0
        ):
            raise ValueError("output_tokens must be non-negative")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of an executor run.

    Invariants:
    - grants_authority MUST always be False. Executor output is evidence only.
    - state must be a terminal state or PAUSED.
    - COMPLETED state cannot have error_code.
    - FAILED / TIMED_OUT states require error_code or error_message.
    - modified_paths must only contain non-empty relative paths without traversal ('..').
    """

    execution_id: str
    adapter_id: str
    state: ExecutorLifecycleState
    summary: str
    modified_paths: tuple[str, ...] = ()
    checkpoints: tuple[ExecutorCheckpoint, ...] = ()
    resource_usage: ExecutorResourceUsage = field(default_factory=ExecutorResourceUsage)
    error_code: str | None = None
    error_message: str | None = None
    vendor_diagnostics: Mapping[str, Any] = field(default_factory=dict)
    containment_profile: str = "uncontained"
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise ValueError("Executor results never confer promotion authority (grants_authority must be False)")

        st = ExecutorLifecycleState(self.state) if isinstance(self.state, str) else self.state
        object.__setattr__(self, "state", st)

        allowed_states = TERMINAL_EXECUTOR_STATES | {ExecutorLifecycleState.PAUSED}
        if st not in allowed_states:
            raise ValueError(f"ExecutionResult state must be terminal or PAUSED, got {st.value}")

        if st == ExecutorLifecycleState.COMPLETED and self.error_code is not None:
            raise ValueError("Completed execution result must not have an error_code")

        if st in {ExecutorLifecycleState.FAILED, ExecutorLifecycleState.TIMED_OUT}:
            if not (self.error_code or self.error_message):
                raise ValueError(f"ExecutionResult in state {st.value} must specify error_code or error_message")

        cleaned_paths: list[str] = []
        for p in self.modified_paths:
            if not isinstance(p, str) or not p.strip():
                raise ValueError(f"Invalid modified path: {p!r} (must be non-empty string)")
            if p.startswith("/") or p.startswith("\\") or Path(p).is_absolute():
                raise ValueError(f"Absolute modified path rejected: {p!r}")
            if ".." in Path(p).parts:
                raise ValueError(f"Path traversal in modified path rejected: {p!r}")
            cleaned_paths.append(p)

        object.__setattr__(self, "modified_paths", tuple(cleaned_paths))
        object.__setattr__(self, "checkpoints", tuple(self.checkpoints))
        object.__setattr__(self, "vendor_diagnostics", MappingProxyType(dict(self.vendor_diagnostics)))


@dataclass(frozen=True, slots=True)
class ExecutorHealthStatus:
    """Runtime availability and health assessment of an executor adapter."""

    adapter_id: str
    healthy: bool
    reason: str | None = None
    runtime_version: str | None = None
    containment_available: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@runtime_checkable
class ExecutorPort(Protocol):
    """Protocol that all executor adapters must implement."""

    def capabilities(self) -> ExecutorCapabilities:
        """Return the declared capabilities of this executor adapter."""
        ...

    async def health(self) -> ExecutorHealthStatus:
        """Assess runtime availability, daemon health, and containment readiness."""
        ...

    async def start(
        self,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        """Initiate execution in the designated candidate worktree."""
        ...

    async def resume(
        self,
        request: ExecutionRequest,
        checkpoint: ExecutorCheckpoint,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        """Resume execution from an earlier checkpoint."""
        ...

    async def cancel(
        self,
        execution_id: str,
        reason: str = "cancelled_by_caller",
    ) -> bool:
        """Request immediate cancellation of an active execution."""
        ...


class ExecutorRegistry:
    """Registry managing available executor adapters."""

    def __init__(self) -> None:
        self._executors: dict[str, ExecutorPort] = {}

    def register(self, executor: ExecutorPort, *, replace: bool = False) -> None:
        adapter_id = executor.capabilities().adapter_id
        if not replace and adapter_id in self._executors:
            raise ValueError(f"Executor with adapter_id {adapter_id!r} is already registered")
        self._executors[adapter_id] = executor

    def get(self, adapter_id: str) -> ExecutorPort:
        if adapter_id not in self._executors:
            raise KeyError(f"Executor adapter_id {adapter_id!r} not found")
        return self._executors[adapter_id]

    def list_adapters(self) -> list[ExecutorCapabilities]:
        return [ex.capabilities() for ex in self._executors.values()]

    def has(self, adapter_id: str) -> bool:
        return adapter_id in self._executors


class DeterministicFakeExecutor:
    """Configurable deterministic fake executor for testing and conformance verification."""

    def __init__(
        self,
        fault_mode: str = "normal",
        adapter_id: str = "deterministic_fake",
        adapter_version: str = "1.0.0",
        supports_resume: bool = True,
        supports_checkpoints: bool = True,
        write_relative_path: str | None = "fake_output.txt",
        write_content: str = "fake output\n",
        supported_network_modes: tuple[NetworkMode, ...] = (NetworkMode.NONE,),
    ) -> None:
        self.fault_mode = fault_mode
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.supports_resume = supports_resume
        self.supports_checkpoints = supports_checkpoints
        self.write_relative_path = write_relative_path
        self.write_content = write_content
        self.supported_network_modes = supported_network_modes
        self.cancelled_executions: set[str] = set()
        self.start_calls: list[ExecutionRequest] = []
        self.resume_calls: list[tuple[ExecutionRequest, ExecutorCheckpoint]] = []

    def capabilities(self) -> ExecutorCapabilities:
        if self.fault_mode == "capability_mismatch":
            return ExecutorCapabilities(
                adapter_id=self.adapter_id,
                adapter_version=self.adapter_version,
                supports_resume=self.supports_resume,
                supports_checkpoints=self.supports_checkpoints,
                supported_network_modes=(NetworkMode.ALLOWLIST,),
            )
        return ExecutorCapabilities(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            supports_resume=self.supports_resume,
            supports_checkpoints=self.supports_checkpoints,
            supported_network_modes=self.supported_network_modes,
        )

    async def health(self) -> ExecutorHealthStatus:
        if self.fault_mode == "health_failure":
            return ExecutorHealthStatus(
                adapter_id=self.adapter_id,
                healthy=False,
                reason="Health check failed: simulated outage",
            )
        return ExecutorHealthStatus(
            adapter_id=self.adapter_id,
            healthy=True,
            runtime_version="test-1.0",
            containment_available=True,
        )

    async def cancel(self, execution_id: str, reason: str = "cancelled_by_caller") -> bool:
        self.cancelled_executions.add(execution_id)
        return True

    async def start(
        self,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        self.start_calls.append(request)

        if self.fault_mode == "cancel_race":
            self.cancelled_executions.add(request.execution_id)
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.CANCELLED,
                summary="Execution cancelled during start race",
                error_code="EXECUTION_CANCELLED",
                error_message="Execution cancelled by race condition",
            )

        if self.fault_mode == "timeout":
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.TIMED_OUT,
                summary="Execution timed out",
                error_code="EXECUTION_TIMEOUT",
                error_message="Execution exceeded timeout limit",
            )

        if self.fault_mode == "crash":
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.FAILED,
                summary="Executor process crashed unexpectedly",
                error_code="EXECUTOR_CRASH",
                error_message="Process terminated with exit code 139 (SIGSEGV)",
            )

        if self.fault_mode == "no_progress":
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.FAILED,
                summary="Execution terminated with zero progress",
                error_code="NO_PROGRESS",
                error_message="No tool invocations or workspace mutations produced",
            )

        if self.fault_mode == "malformed_result":
            res = object.__new__(ExecutionResult)
            object.__setattr__(res, "execution_id", request.execution_id)
            object.__setattr__(res, "adapter_id", self.adapter_id)
            object.__setattr__(res, "state", ExecutorLifecycleState.COMPLETED)
            object.__setattr__(res, "summary", "Malformed completed result with error")
            object.__setattr__(res, "modified_paths", ())
            object.__setattr__(res, "checkpoints", ())
            object.__setattr__(res, "resource_usage", ExecutorResourceUsage())
            object.__setattr__(res, "error_code", "MALFORMED_ERROR_CODE")
            object.__setattr__(res, "error_message", "Completed should not have error")
            object.__setattr__(res, "vendor_diagnostics", MappingProxyType({}))
            object.__setattr__(res, "containment_profile", "uncontained")
            object.__setattr__(res, "grants_authority", False)
            return res

        if self.fault_mode == "capability_mismatch":
            if request.resource_limits.network_mode not in self.capabilities().supported_network_modes:
                return ExecutionResult(
                    execution_id=request.execution_id,
                    adapter_id=self.adapter_id,
                    state=ExecutorLifecycleState.FAILED,
                    summary="Network mode not supported by adapter",
                    error_code="CAPABILITY_MISMATCH",
                    error_message=f"Requested {request.resource_limits.network_mode.value} not supported",
                )

        if self.fault_mode == "canonical_write_attempt":
            if request.canonical_root is not None:
                breach_file = request.canonical_root / "unauthorized_breach.txt"
                breach_file.write_text("breach\n", encoding="utf-8")
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.COMPLETED,
                summary="Breached canonical root",
                modified_paths=(),
            )

        if self.fault_mode == "pause_and_resume":
            cp = ExecutorCheckpoint(
                checkpoint_id=f"chk_{request.execution_id}_pause",
                execution_id=request.execution_id,
                sequence=1,
                summary="Execution paused for human approval",
                created_at="2026-11-16T00:00:00Z",
                resumable_token=f"token_{request.execution_id}",
            )
            if on_checkpoint is not None and self.supports_checkpoints:
                on_checkpoint(cp)
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.PAUSED,
                summary="Execution paused",
                checkpoints=(cp,),
            )

        # Normal execution
        checkpoints: list[ExecutorCheckpoint] = []
        if self.supports_checkpoints:
            cp = ExecutorCheckpoint(
                checkpoint_id=f"chk_{request.execution_id}_init",
                execution_id=request.execution_id,
                sequence=1,
                summary="Execution started and initialized environment",
                created_at="2026-11-16T00:00:00Z",
            )
            checkpoints.append(cp)
            if on_checkpoint is not None:
                on_checkpoint(cp)

        modified_paths: list[str] = []
        if self.write_relative_path:
            dest = request.candidate_root / self.write_relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(self.write_content, encoding="utf-8")
            modified_paths.append(self.write_relative_path)

        return ExecutionResult(
            execution_id=request.execution_id,
            adapter_id=self.adapter_id,
            state=ExecutorLifecycleState.COMPLETED,
            summary="Execution completed successfully",
            modified_paths=tuple(modified_paths),
            checkpoints=tuple(checkpoints),
            resource_usage=ExecutorResourceUsage(
                duration_ms=15,
                turns_used=1,
                input_tokens=100,
                output_tokens=50,
                exit_code=0,
            ),
        )

    async def resume(
        self,
        request: ExecutionRequest,
        checkpoint: ExecutorCheckpoint,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        self.resume_calls.append((request, checkpoint))

        if not self.supports_resume:
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self.adapter_id,
                state=ExecutorLifecycleState.FAILED,
                summary="Resume operation not supported by adapter",
                error_code="RESUME_NOT_SUPPORTED",
                error_message="Adapter does not support execution resumption",
            )

        checkpoints: list[ExecutorCheckpoint] = []
        if self.supports_checkpoints:
            cp = ExecutorCheckpoint(
                checkpoint_id=f"chk_{request.execution_id}_resumed",
                execution_id=request.execution_id,
                sequence=checkpoint.sequence + 1,
                summary="Execution resumed from checkpoint",
                created_at="2026-11-16T00:00:01Z",
            )
            checkpoints.append(cp)
            if on_checkpoint is not None:
                on_checkpoint(cp)

        modified_paths: list[str] = []
        if self.write_relative_path:
            dest = request.candidate_root / self.write_relative_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(self.write_content, encoding="utf-8")
            modified_paths.append(self.write_relative_path)

        return ExecutionResult(
            execution_id=request.execution_id,
            adapter_id=self.adapter_id,
            state=ExecutorLifecycleState.COMPLETED,
            summary="Resumed execution completed successfully",
            modified_paths=tuple(modified_paths),
            checkpoints=tuple(checkpoints),
            resource_usage=ExecutorResourceUsage(
                duration_ms=25,
                turns_used=2,
                input_tokens=150,
                output_tokens=75,
                exit_code=0,
            ),
        )


@dataclass(frozen=True, slots=True)
class ConformanceCheckResult:
    """Individual conformance check assertion result."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExecutorCertificationReport:
    """Consolidated certification report for an executor adapter."""

    adapter_id: str
    passed: bool
    checks: tuple[ConformanceCheckResult, ...] = ()
    failures: tuple[str, ...] = ()


def _snapshot_directory(root: Path) -> dict[str, str]:
    """Capture relative file paths and their sha256 hashes."""
    if not root.exists():
        return {}
    snapshot: dict[str, str] = {}
    for p in root.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(root))
            snapshot[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snapshot


async def run_executor_conformance_suite(
    factory: Callable[[], ExecutorPort],
    workspace_root: Path,
) -> ExecutorCertificationReport:
    """Run the comprehensive conformance and certification suite against an executor.

    Validates:
    1. Protocol implementation adherence (ExecutorPort).
    2. Capabilities structure and invariants.
    3. Health check responsiveness and status.
    4. Execution lifecycle, checkpoints, authority boundary, and canonical immutability.
    5. Resumption semantics for resume-capable and non-resumable adapters.
    6. Cancellation responsiveness and error safety.
    7. Parameter and path security boundary enforcement.
    """
    checks: list[ConformanceCheckResult] = []
    failures: list[str] = []

    def record_check(name: str, passed: bool, detail: str = "") -> None:
        checks.append(ConformanceCheckResult(name=name, passed=passed, detail=detail))
        if not passed:
            failures.append(f"{name}: {detail}" if detail else name)

    # 1. Protocol implementation
    try:
        adapter = factory()
        if not isinstance(adapter, ExecutorPort):
            record_check("protocol_implementation", False, f"{type(adapter)} does not implement ExecutorPort")
        else:
            record_check("protocol_implementation", True)
    except Exception as exc:
        record_check("protocol_implementation", False, f"Factory instantiation raised: {exc}")
        return ExecutorCertificationReport(
            adapter_id="unknown",
            passed=False,
            checks=tuple(checks),
            failures=tuple(failures),
        )

    # 2. Capabilities declaration
    try:
        caps = adapter.capabilities()
        if not isinstance(caps, ExecutorCapabilities):
            record_check("capabilities_declaration", False, "capabilities() did not return ExecutorCapabilities")
        elif not caps.adapter_id or not caps.adapter_version:
            record_check("capabilities_declaration", False, "Missing adapter_id or adapter_version")
        elif caps.max_concurrent_executions < 1:
            record_check("capabilities_declaration", False, "max_concurrent_executions must be >= 1")
        elif not caps.supported_network_modes:
            record_check("capabilities_declaration", False, "supported_network_modes must not be empty")
        else:
            record_check("capabilities_declaration", True)
    except Exception as exc:
        record_check("capabilities_declaration", False, f"capabilities() failed: {exc}")
        caps = ExecutorCapabilities(adapter_id="unidentified", adapter_version="0.0")

    adapter_id = caps.adapter_id

    # 3. Health check
    try:
        health = await adapter.health()
        if not isinstance(health, ExecutorHealthStatus):
            record_check("health_check", False, "health() did not return ExecutorHealthStatus")
        elif health.adapter_id != caps.adapter_id:
            record_check("health_check", False, f"Health adapter_id {health.adapter_id!r} != {caps.adapter_id!r}")
        elif not health.healthy:
            record_check("health_check", False, f"Adapter reported unhealthy: {health.reason}")
        else:
            record_check("health_check", True)
    except Exception as exc:
        record_check("health_check", False, f"health() failed: {exc}")

    # 4. Lifecycle execution, immutability, and checkpoint test
    try:
        suite_run_id = uuid.uuid4().hex[:8]
        candidate_dir = workspace_root / f"cand_{suite_run_id}"
        canonical_dir = workspace_root / f"canon_{suite_run_id}"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        canonical_dir.mkdir(parents=True, exist_ok=True)

        canary_file = canonical_dir / "canary.txt"
        canary_file.write_text("canonical source of truth\n", encoding="utf-8")
        canonical_snapshot_before = _snapshot_directory(canonical_dir)

        captured_checkpoints: list[ExecutorCheckpoint] = []
        chosen_net_mode = caps.supported_network_modes[0]

        request = ExecutionRequest(
            execution_id=f"exec_{suite_run_id}",
            project_id="test_project",
            job_id="test_job",
            candidate_root=candidate_dir,
            canonical_root=canonical_dir,
            mandate="Implement conformant change",
            resource_limits=ExecutorResourceLimits(network_mode=chosen_net_mode),
        )

        result = await adapter.start(request, on_checkpoint=captured_checkpoints.append)

        # Invariant checks on result
        if not isinstance(result, ExecutionResult):
            record_check("execution_lifecycle", False, f"start() did not return ExecutionResult, got {type(result)}")
        elif result.grants_authority:
            record_check("execution_lifecycle", False, "Result violated security boundary: grants_authority is True")
        elif result.execution_id != request.execution_id:
            record_check("execution_lifecycle", False, f"Result execution_id {result.execution_id} != {request.execution_id}")
        elif result.adapter_id != caps.adapter_id:
            record_check("execution_lifecycle", False, f"Result adapter_id {result.adapter_id} != {caps.adapter_id}")
        elif result.state == ExecutorLifecycleState.PAUSED:
            if not caps.supports_resume:
                record_check("execution_lifecycle", False, "Adapter returned PAUSED state but does not support resume")
            elif not result.checkpoints:
                record_check("execution_lifecycle", False, "Paused result missing checkpoints for resumption")
            else:
                # Test resume to completion
                resumed = await adapter.resume(request, result.checkpoints[-1], on_checkpoint=captured_checkpoints.append)
                if resumed.state != ExecutorLifecycleState.COMPLETED:
                    record_check("execution_lifecycle", False, f"Resumed execution did not reach COMPLETED, got {resumed.state.value}")
                elif resumed.grants_authority:
                    record_check("execution_lifecycle", False, "Resumed result confers authority")
                else:
                    record_check("execution_lifecycle", True)
        elif result.state != ExecutorLifecycleState.COMPLETED:
            record_check("execution_lifecycle", False, f"Execution ended in unexpected non-completed state: {result.state.value}")
        else:
            if result.error_code is not None:
                record_check("execution_lifecycle", False, f"Completed result has error_code: {result.error_code}")
            else:
                record_check("execution_lifecycle", True)

        # Check modified paths stay within candidate
        for p in result.modified_paths:
            resolved_mod = (candidate_dir / p).resolve()
            if not str(resolved_mod).startswith(str(candidate_dir.resolve())):
                record_check("isolation_containment", False, f"Modified path escaped candidate root: {p}")
                break
        else:
            record_check("isolation_containment", True)

        # Check canonical root immutability
        canonical_snapshot_after = _snapshot_directory(canonical_dir)
        if canonical_snapshot_before != canonical_snapshot_after:
            record_check("canonical_immutability", False, "Canonical repository was modified during execution!")
        else:
            record_check("canonical_immutability", True)

        # Check checkpoint emission
        if caps.supports_checkpoints:
            if not captured_checkpoints and not result.checkpoints:
                record_check("checkpoint_emission", False, "Adapter supports checkpoints but none were emitted")
            else:
                for cp in captured_checkpoints:
                    if not isinstance(cp, ExecutorCheckpoint) or cp.execution_id != request.execution_id:
                        record_check("checkpoint_emission", False, f"Invalid checkpoint emitted: {cp}")
                        break
                else:
                    record_check("checkpoint_emission", True)
        else:
            record_check("checkpoint_emission", True)

    except Exception as exc:
        record_check("execution_lifecycle", False, f"Execution lifecycle test raised: {exc}")

    # 5. Resumption semantics
    try:
        resume_req = ExecutionRequest(
            execution_id=f"exec_res_{uuid.uuid4().hex[:6]}",
            project_id="test_project",
            job_id="test_job",
            candidate_root=workspace_root / f"cand_res_{uuid.uuid4().hex[:6]}",
            mandate="Resume check",
        )
        resume_req.candidate_root.mkdir(parents=True, exist_ok=True)
        dummy_cp = ExecutorCheckpoint(
            checkpoint_id="chk_dummy",
            execution_id=resume_req.execution_id,
            sequence=1,
            summary="Dummy checkpoint",
            created_at="2026-11-16T00:00:00Z",
        )
        resume_res = await adapter.resume(resume_req, dummy_cp)
        if caps.supports_resume:
            if not isinstance(resume_res, ExecutionResult) or resume_res.grants_authority:
                record_check("resumption_semantics", False, "Resume returned invalid result or conferred authority")
            else:
                record_check("resumption_semantics", True)
        else:
            if resume_res.state == ExecutorLifecycleState.COMPLETED and not resume_res.error_code:
                record_check("resumption_semantics", False, "Adapter claimed no resume support but returned COMPLETED on resume")
            else:
                record_check("resumption_semantics", True)
    except Exception as exc:
        if not caps.supports_resume:
            record_check("resumption_semantics", True)
        else:
            record_check("resumption_semantics", False, f"Resume test raised: {exc}")

    # 6. Cancellation responsiveness
    try:
        cancel_id = f"cancel_test_{uuid.uuid4().hex[:6]}"
        cancelled = await adapter.cancel(cancel_id, reason="conformance_test")
        if not isinstance(cancelled, bool):
            record_check("cancellation_safety", False, f"cancel() returned non-bool: {type(cancelled)}")
        else:
            record_check("cancellation_safety", True)
    except Exception as exc:
        record_check("cancellation_safety", False, f"cancel() raised unhandled exception: {exc}")

    # 7. Security gates and invariant enforcement
    try:
        # Check candidate == canonical rejection
        same_dir = workspace_root / "same_dir"
        same_dir.mkdir(parents=True, exist_ok=True)
        try:
            ExecutionRequest(
                execution_id="gate_1",
                project_id="p",
                job_id="j",
                candidate_root=same_dir,
                canonical_root=same_dir,
                mandate="forbidden",
            )
            record_check("security_gates", False, "ExecutionRequest allowed candidate_root == canonical_root")
        except ValueError:
            record_check("security_gates", True)

        # Check grants_authority rejection
        try:
            ExecutionResult(
                execution_id="gate_2",
                adapter_id=caps.adapter_id,
                state=ExecutorLifecycleState.COMPLETED,
                summary="forbidden",
                grants_authority=True,
            )
            record_check("security_gate_authority", False, "ExecutionResult allowed grants_authority=True")
        except ValueError:
            record_check("security_gate_authority", True)

        # Check path traversal rejection
        try:
            ExecutionResult(
                execution_id="gate_3",
                adapter_id=caps.adapter_id,
                state=ExecutorLifecycleState.COMPLETED,
                summary="traversal",
                modified_paths=("../escape.txt",),
            )
            record_check("security_gate_traversal", False, "ExecutionResult allowed path traversal")
        except ValueError:
            record_check("security_gate_traversal", True)

    except Exception as exc:
        record_check("security_gates", False, f"Security gate verification raised: {exc}")

    passed = len(failures) == 0
    return ExecutorCertificationReport(
        adapter_id=adapter_id,
        passed=passed,
        checks=tuple(checks),
        failures=tuple(failures),
    )


def run_executor_conformance_suite_sync(
    factory: Callable[[], ExecutorPort],
    workspace_root: Path,
) -> ExecutorCertificationReport:
    """Synchronous convenience wrapper for ``run_executor_conformance_suite``."""
    try:
        asyncio.get_running_loop()
        # If already running inside an active event loop, execute in dedicated thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(run_executor_conformance_suite(factory, workspace_root))
            ).result()
    except RuntimeError:
        return asyncio.run(run_executor_conformance_suite(factory, workspace_root))
